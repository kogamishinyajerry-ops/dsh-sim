"""可靠性证据 R1：双进程并发 claim 压测（真实进程 + 真实 SQLite 文件库）。

场景：同一 SQLite 文件库，主进程入队 N 个 PREPARE 作业，spawn 2 个独立子进程
（各自独立 engine/SessionFactory + BEGIN IMMEDIATE）并发循环 claim 直到队空。

断言（全部真实验证，不编造）：
1. 无重复分配：每个 job_id 恰被一个进程拿到一次（两进程结果汇总查重）
2. 无遗漏：N 个作业全部被领走
3. 租约唯一：leases 表每 job 恰一条活跃租约；fencing_token 全局不重复
4. SQLite 锁等待不炸：子进程对 "database is locked" OperationalError 计数重试

环境补丁（如实注明）：db/session.make_engine 的 connect_args 只配了
check_same_thread，未配 timeout 与 journal_mode。本脚本对每个连接
（主/子进程）显式 PRAGMA journal_mode=WAL + busy_timeout=5000ms。
工程 make_engine 是否补齐属工程侧决定，此处不改服务代码。

用法：
  <PYTHON_ENV>\\Scripts\\python.exe
      scripts/stress_concurrent_claim.py [N]
"""
from __future__ import annotations

import json
import multiprocessing as mp
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 子进程 worker：独立 engine/SessionFactory，循环 claim 直到队空
# ---------------------------------------------------------------------------


def _read_tables(db_path: str):
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        lease_rows = conn.execute(
            "SELECT job_id, fencing_token, active FROM leases"
        ).fetchall()
        job_rows = conn.execute(
            "SELECT job_id, fencing_counter, state FROM jobs"
        ).fetchall()
    finally:
        conn.close()
    return lease_rows, job_rows


def _claim_worker(db_path: str, node_id: str, out_file: str) -> None:
    """独立进程：独立连接池对同一 sqlite 文件库循环 claim。

    NodeRow 首次 claim 自动登记时 max_concurrent 取列默认 1（配额逻辑，非本压测
    目标），子进程先直接 sqlite3 upsert NodeRow.max_concurrent=1000 放开配额，
    再走 service.claim 真实路径。

    结果经 JSON 文件回传（Windows spawn 下 mp.Queue feeder 线程会拖住子进程
    退出使 join 等满超时；文件回传无此问题）。
    """
    out_path = Path(out_file)

    def _emit(err_extra: list[str] | None = None) -> None:
        try:
            lr, jr = _read_tables(db_path)
        except Exception:
            lr, jr = [], []
        out_path.write_text(
            json.dumps(
                {
                    "node_id": node_id,
                    "claimed": results,
                    "lock_retries": lock_retries,
                    "errors": errors + (err_extra or []),
                    "lease_rows": lr,
                    "job_rows": jr,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    lock_retries = 0
    results: list[dict] = []
    errors: list[str] = []
    engine = None
    try:
        from sqlalchemy import create_engine, event

        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            future=True,
        )

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        @event.listens_for(engine, "begin")
        def _begin_immediate(conn):  # noqa: ANN001
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        with conn:
            conn.execute(
                "INSERT INTO nodes(node_id, capabilities, max_concurrent, registered_at) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(node_id) "
                "DO UPDATE SET max_concurrent=excluded.max_concurrent",
                (node_id, "{}", 1000, datetime.now(timezone.utc).isoformat()),
            )
        conn.close()

        from dsh_sim.queue import service as queue_service
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(engine, expire_on_commit=False, future=True)

        idle_streak = 0
        while idle_streak < 3:  # 连续 3 轮空转才认队空（另一进程可能并发中）
            session = factory()
            try:
                job, lease = queue_service.claim(session, node_id=node_id)
                session.commit()
            except Exception as exc:
                session.rollback()
                msg = f"{getattr(exc, 'orig', '') or exc}"
                if "database is locked" in str(msg).lower():
                    lock_retries += 1
                    time.sleep(0.02 + 0.01 * (lock_retries % 5))
                    continue
                errors.append(f"{type(exc).__name__}: {exc}")
                break
            finally:
                session.close()
            if job is None or lease is None:
                idle_streak += 1
                time.sleep(0.02)
                continue
            idle_streak = 0
            results.append(
                {
                    "job_id": job.job_id,
                    "lease_id": lease.lease_id,
                    "fencing_token": lease.fencing_token,
                }
            )
        lease_rows, job_rows = _read_tables(db_path)
        out_path.write_text(
            json.dumps(
                {
                    "node_id": node_id,
                    "claimed": results,
                    "lock_retries": lock_retries,
                    "errors": errors,
                    "lease_rows": lease_rows,
                    "job_rows": job_rows,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # 兜底：任何崩溃也必须落盘，主进程才不会挂死
        _emit([f"FATAL {type(exc).__name__}: {exc}"])
    finally:
        if engine is not None:
            engine.dispose()


# ---------------------------------------------------------------------------
# 主进程：建库 → 入队 N → spawn 2 子进程 → 汇总断言
# ---------------------------------------------------------------------------


def main() -> int:
    n_jobs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    tmpdir = Path(tempfile.mkdtemp(prefix="dsh_sim_stress_"))
    db_path = tmpdir / "stress.db"

    t0 = time.time()

    from dsh_sim.db.models import Base
    from dsh_sim.db.session import make_engine, make_session_factory
    from dsh_sim.queue import service as queue_service

    engine = make_engine(f"sqlite:///{db_path.as_posix()}")
    # 环境补丁：make_engine 未配 timeout/journal_mode —— 主进程侧补上（见文件头）
    raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA busy_timeout=5000")
    raw.close()

    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    expected: list[str] = []
    with factory() as s:
        for _ in range(n_jobs):
            job = queue_service.enqueue(s, kind="PREPARE", task_id=None)
            expected.append(job.job_id)
        s.commit()
    print(f"[setup] db={db_path}")
    print(f"[setup] enqueued N={n_jobs} PREPARE jobs (QUEUED)")

    ctx = mp.get_context("spawn")
    out1, out2 = tmpdir / "node1.json", tmpdir / "node2.json"
    p1 = ctx.Process(target=_claim_worker, args=(str(db_path), "stress-node-1", str(out1)))
    p2 = ctx.Process(target=_claim_worker, args=(str(db_path), "stress-node-2", str(out2)))
    p1.start()
    p2.start()
    p1.join(timeout=180)
    p2.join(timeout=180)

    def _load(f: Path, label: str) -> dict | None:
        for _ in range(20):  # 进程退出后文件应立即存在；小退避兜底
            if f.is_file():
                try:
                    return json.loads(f.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            time.sleep(0.25)
        print(f"[FATAL] {label} 未落盘结果（进程崩溃且兜底失效）")
        return None

    r1 = _load(out1, "node-1")
    r2 = _load(out2, "node-2")
    engine.dispose()
    if r1 is None or r2 is None:
        for p in (p1, p2):
            if p.is_alive():
                p.kill()
        return 2

    # ---- 断言 1：无重复分配 ----
    all_claims = r1["claimed"] + r2["claimed"]
    job_counts = Counter(c["job_id"] for c in all_claims)
    dup_jobs = {j: c for j, c in job_counts.items() if c > 1}

    # ---- 断言 2：无遗漏 ----
    claimed_set = set(job_counts)
    missing = sorted(set(expected) - claimed_set)
    extra = sorted(claimed_set - set(expected))

    # ---- 断言 3：租约唯一 / fencing_token ----
    # 实现语义（queue/service.claim）：token = job.fencing_counter，**job 级单调**，
    # 跨 job 允许同值（首租全为 1）。故断言口径为：每 job 恰 1 条活跃租约、
    # 每 job 的 token 集合无重复；"全局单调递增"与实现的口径差记 FINDING。
    lease_rows = r1["lease_rows"] or r2["lease_rows"]  # 同一张表，两份读数任取
    lease_counter: Counter = Counter()
    tokens_by_job: dict[str, list[int]] = {}
    for job_id, token, active in lease_rows:
        if active:
            lease_counter[job_id] += 1
            tokens_by_job.setdefault(job_id, []).append(token)
    multi_active = {j: c for j, c in lease_counter.items() if c > 1}
    token_dup_per_job = any(len(toks) != len(set(toks)) for toks in tokens_by_job.values())
    job_rows = r1["job_rows"] or r2["job_rows"]
    job_counter_map = {jid: counter for jid, counter, _st in job_rows}
    # counter==token（首租自 1）
    monotonic_ok = all(
        sorted(toks) == [job_counter_map.get(jid, -1)]
        for jid, toks in tokens_by_job.items()
    )
    all_first_lease = all(t == 1 for ts in tokens_by_job.values() for t in ts)

    # ---- 补充验证：重派后 token 递增（单进程白盒段）----
    # 直接把 M 个已领作业 state 重置回 QUEUED（模拟作业重新入队），
    # 再走真实 claim：fencing_counter 应 0→1→2 递增，旧活跃租约仍保留历史。
    m_release = 3
    requeue_ids = [c["job_id"] for c in all_claims[:m_release]]
    reok = sqlite3.connect(db_path, timeout=10.0)
    reok.execute("PRAGMA busy_timeout=5000")
    with reok:
        for jid in requeue_ids:
            reok.execute(
                "UPDATE jobs SET state='QUEUED' WHERE job_id=?", (jid,)
            )
    reok.close()
    requeue_tokens: dict[str, list[int]] = {}
    from dsh_sim.queue import service as qs_re

    with factory() as s2:
        for jid in requeue_ids:
            job2, lease2 = qs_re.claim(s2, node_id="stress-node-1")
            s2.commit()
            if job2 is not None and lease2 is not None:
                requeue_tokens[job2.job_id] = [lease2.fencing_token]
    token_increments_ok = (
        len(requeue_tokens) == m_release
        and all(toks == [2] for toks in requeue_tokens.values())
    )

    # ---- 断言 4：锁重试统计 ----
    lock_retries_total = r1["lock_retries"] + r2["lock_retries"]
    errors = r1["errors"] + r2["errors"]
    unleased = [jid for jid in expected if jid not in tokens_by_job]

    elapsed = time.time() - t0
    print()
    print("=" * 62)
    print(f"并发 claim 压测汇总  (N={n_jobs}, 进程数=2, 耗时 {elapsed:.2f}s)")
    print("=" * 62)
    print(
        f"node-1 领取 {len(r1['claimed']):>3} | node-2 领取 {len(r2['claimed']):>3}"
        f" | 合计 {len(all_claims)}"
    )
    print(f"重复分配   : {len(dup_jobs)} {'(FAIL)' if dup_jobs else '(OK)'}")
    print(f"遗漏       : {len(missing)} {'(FAIL)' if missing else '(OK)'}")
    if missing:
        print(f"  missing: {missing[:5]}")
    if extra:
        print(f"  unexpected: {extra[:5]}")
    if unleased:
        print(f"  入队但无活跃租约: {len(unleased)} {unleased[:5]}")
    print(f"多活跃租约 : {len(multi_active)} {'(FAIL)' if multi_active else '(OK)'}")
    print(
        f"fencing_token 每 job 不重复: {'OK' if not token_dup_per_job else 'FAIL'}"
        f" (active={sum(len(v) for v in tokens_by_job.values())},"
        f" 首租 token 全为 1: {'是' if all_first_lease else '否'})"
    )
    print(f"fencing_counter==token（job 级单调自 1）: {'OK' if monotonic_ok else 'FAIL'}")
    print(
        f"重派验证   : {len(requeue_tokens)}/{m_release} 作业二次出租 token=2"
        f" {'(OK)' if token_increments_ok else '(FAIL)'}"
    )
    sample = sorted(((c["job_id"][-8:], c["fencing_token"]) for c in all_claims))[:5]
    print(f"  领取抽样(job 后 8 位,token): {sample}")
    print(
        "  FINDING-1: fencing_token 为 job 级单调（跨 job 可同值，首租全为 1），"
        "非全局唯一序号；数据库唯一约束在 (job_id,event_seq)/lease_id，不在跨 job token。"
    )
    print(
        f"锁等待重试 : node-1={r1['lock_retries']} node-2={r2['lock_retries']}"
        f" 合计={lock_retries_total}（WAL+busy_timeout=5s 下被阻塞时自动等待而非报错）"
    )
    print(f"子进程错误 : {errors if errors else '无'}")

    passed = (
        not dup_jobs
        and not missing
        and not extra
        and not multi_active
        and not token_dup_per_job
        and monotonic_ok
        and token_increments_ok
        and not errors
        and not unleased
    )
    print("=" * 62)
    print(f"结论: {'PASS' if passed else 'FAIL'}")
    print(f"  db: {db_path}")
    print("  注: make_engine 未配 timeout/WAL——脚本内已对每个连接设置")
    print("      PRAGMA journal_mode=WAL + busy_timeout=5000（见脚本头部说明）")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
