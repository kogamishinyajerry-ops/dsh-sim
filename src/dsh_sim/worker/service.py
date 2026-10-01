"""常驻 Worker 服务入口（受控最小实现；修复断点 C）。

问题：`run_until_idle()` 在队列为空时退出，`python -m uvicorn` 启动 API 不会
自动带起常驻 worker，自然语言流程在"准备/求解异步受理"之后没人继续领取作业。

本模块提供**最小受控常驻入口** `python -m dsh_sim.worker.service`：

- 复用既有 `run_until_idle` 的同一领取/处理实现（queue.claim + PREPARE/EXECUTE
  分发 + WAL），不另建第二套队列语义；
- 空队列时结束会话（释放 SQLite BEGIN IMMEDIATE 保留锁）并按
  `DSH_SIM_WORKER_IDLE_POLL_SECONDS`（默认 2s，上限=心跳间隔）等待后继续领取；
- SIGTERM / SIGINT 优雅停止：完成当前作业后不再领取新作业；再次收到信号按
  KeyboardInterrupt 路径立即退出（由 run_until_idle 的异常清理兜底冻结现场）；
- 结构化日志走 stdout（node_id、claim、idle、stop），可被容器/面板直接 tail；
- adapter 由 `DSH_SIM_WORKER_ADAPTER` 显式选择（mock/cli/openfoam），openfoam
  需要显式模板 registry/root、workdir、artifact root 与同一数据库 URL。

诚实边界：这是"持续运行的领取循环"，**不是**崩溃后自动接管（worker 被杀死后
在运行作业转 LOST 待人工核实，重启后不会自动续跑未完成求解）——该能力仍未
实现，见 docs/openfoam-validation.md §进程与恢复边界。
"""
from __future__ import annotations

import os
import signal
import threading
import time
from typing import Callable

from dsh_sim.db.session import database_url, init_db, make_engine, make_session_factory
from dsh_sim.queue.service import HEARTBEAT_INTERVAL_SECONDS
from dsh_sim.worker.loop import WorkerConfig, make_adapter_from_env, run_until_idle

ENV_IDLE_POLL = "DSH_SIM_WORKER_IDLE_POLL_SECONDS"
DEFAULT_IDLE_POLL_SECONDS = 2.0


class StopFlag:
    """线程安全的停止标志；信号处理器与测试都只调用 request_stop()。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def request_stop(self) -> None:
        self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


def idle_poll_seconds(environ: dict[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    raw = env.get(ENV_IDLE_POLL)
    value = float(raw) if raw else DEFAULT_IDLE_POLL_SECONDS
    if not 0 <= value <= HEARTBEAT_INTERVAL_SECONDS:
        raise ValueError(
            f"{ENV_IDLE_POLL} 必须在 0 与心跳间隔 {HEARTBEAT_INTERVAL_SECONDS}s 之间"
        )
    return value


def _log(message: str) -> None:
    """单行结构化日志；stdout 每行 flush，便于容器/面板直接 tail。"""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    print(f"[dsh-sim-worker {stamp}] {message}", flush=True)


def _install_signal_handlers(stop: StopFlag, log: Callable[[str], None]) -> bool:
    """主线程安装 SIGTERM/SIGINT 优雅停止；非主线程返回 False（测试场景）。"""

    def _handle(signum: int, _frame: object) -> None:
        if stop.requested:
            # 第二次信号：立即退出；当前受控进程组的清理交给异常路径兜底。
            log(f"received signal {signum} again; exiting now (in-flight cleanup is best effort)")
            raise KeyboardInterrupt(f"signal {signum}")
        stop.request_stop()
        log(f"received signal {signum}; will stop after the current job (no new claims)")

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _handle)
    except ValueError:
        return False  # 非主线程：信号由调用方管理
    return True


def serve(
    config: WorkerConfig | None = None,
    *,
    stop: StopFlag | None = None,
    log: Callable[[str], None] = _log,
    install_signals: bool = True,
) -> None:
    """常驻领取循环：空队列等待后继续；信号优雅停止；stop 标志可注入（测试）。"""
    flag = stop or StopFlag()
    cfg = config or WorkerConfig.from_env()
    engine = make_engine(database_url())
    init_db(engine)
    factory = make_session_factory(engine)
    adapter = make_adapter_from_env()
    idle_wait = idle_poll_seconds()
    if install_signals:
        _install_signal_handlers(flag, log)
    log(
        f"worker started: node={cfg.node_id} adapter={type(adapter).__name__} "
        f"db={database_url()} work_root={cfg.work_root} artifact_root={cfg.artifact_root} "
        f"idle_poll={idle_wait}s"
    )
    total = 0
    while not flag.requested:
        with factory() as session:
            # 领取边界检查停止：停止请求不打断当前作业，只保证剩余作业不被领取。
            processed = run_until_idle(session, adapter, cfg, should_stop=lambda: flag.requested)
        if processed:
            total += len(processed)
            log(f"processed {len(processed)} job(s): {', '.join(processed)} (total={total})")
            continue  # 立即再试：队列可能仍有积压
        # 空队列：会话已随 with 块提交/关闭（释放写事务），再等待。
        log(f"queue idle; sleeping {idle_wait}s")
        _sleep_interruptible(idle_wait, flag)
    log(f"worker stopped gracefully; total processed={total} (unclaimed jobs remain queued)")


def _sleep_interruptible(seconds: float, stop: StopFlag) -> None:
    """分片睡眠：停止请求到达后最迟一个分片内响应，不长时间阻塞退出。"""
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    slice_seconds = min(0.5, seconds)
    while not stop.requested and time.monotonic() < deadline:
        stop.wait(min(slice_seconds, max(0.0, deadline - time.monotonic())))


def main() -> None:
    try:
        serve()
    except ValueError as exc:
        _log(f"configuration error: {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
