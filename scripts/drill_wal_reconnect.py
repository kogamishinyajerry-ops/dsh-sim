"""可靠性证据 R2：Worker WAL 真断连重传演练（API 子进程 SIGKILL）。

场景：模拟 Worker 在 API 不可达期间产生事件 → 恢复后按序重传，不丢不重。

步骤（全部真实执行）：
1. 临时 SQLite 库起 API（uvicorn 子进程，端口 8612，避开工程 8600/3080）
   → 直接造 JobRow（EXECUTE，链路搭建成本高，按演练预案降级为直造 job + claim）
   → NODE_ADMIN 走 HTTP /jobs/claim 真实领取，拿 lease_id/fencing_token
2. Worker 侧 JsonlWal：append seq1 STARTING / seq2 RUNNING / seq3 HEARTBEAT，
   仅 mark_acked seq1（seq1 已通过 HTTP 真实送达并落 events 表）
3. SIGKILL 杀 API → Worker 再 append seq4 COMPLETED —— 验证 API 死时 WAL 仍可写
4. 重启 API → unacked() 取 seq2/3/4 按序经 HTTP post_event 重传
   → 断言 events 表 4 条全在、顺序正确、无重复；重传后 unacked() 为空
5. 幂等性：故意把 seq2 再传第二遍 → 如实记录 API 实际行为与预期是否一致；
   不一致标 FINDING，不改服务代码。

用法：
  <PYTHON_ENV>\\Scripts\\python.exe
      scripts/drill_wal_reconnect.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PY = sys.executable
REPO = Path(__file__).resolve().parents[1]
PORT = 8612
sys.path.insert(0, str(REPO))

FINDINGS: list[str] = []
_steps: list[str] = []


def step(msg: str) -> None:
    _steps.append(msg)
    print(f"  {msg}")


def call(method: str, path: str, body=None, subject="worker-node", roles="NODE_ADMIN", idem: str | None = None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/v1{path}", method=method
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Dev-Subject", subject)
    req.add_header("X-Dev-Roles", roles)
    req.add_header("X-Dev-Projects", "drill-proj")
    if idem:
        req.add_header("Idempotency-Key", idem)
    if body is not None:
        req.data = body if isinstance(body, bytes) else json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def wait_up(timeout=40) -> bool:
    for _ in range(timeout * 2):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/api/v1/capabilities", timeout=3
            ) as r:
                if r.status in (200, 403):
                    return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def wait_down(port: int, timeout=15) -> bool:
    for _ in range(timeout * 2):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
        except urllib.error.HTTPError:
            return False  # 还有响应
        except Exception:
            return True
        time.sleep(0.5)
    return False


def start_api(db_url: str):
    env = dict(os.environ)
    env["DSH_SIM_DATABASE_URL"] = db_url
    env["DSH_SIM_ARTIFACT_ROOT"] = str(tmpdir / "artifacts")
    return subprocess.Popen(
        [
            PY, "-m", "uvicorn", "dsh_sim.api.main:app",
            "--port", str(PORT), "--log-level", "error",
        ],
        cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def read_events(db_path: str):
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        return conn.execute(
            "SELECT event_seq, kind FROM events WHERE job_id=? ORDER BY event_seq",
            (JOB_ID,),
        ).fetchall()
    finally:
        conn.close()


JOB_ID: str = ""  # main 里赋值


def main() -> int:
    global JOB_ID, tmpdir
    tmpdir = Path(tempfile.mkdtemp(prefix="dsh_sim_drill_"))
    db_path = tmpdir / "drill.db"
    db_url = f"sqlite:///{db_path.as_posix()}"

    # 库需先存在并有表，API 启动时 init_db 也会建，但我们要在起 API 前造 JobRow
    from dsh_sim.db.models import Base, JobRow
    from dsh_sim.db.session import make_engine, make_session_factory

    engine = make_engine(db_url)
    raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA busy_timeout=5000")
    raw.close()
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        job = JobRow(job_id="job_drill_exec_0001", kind="EXECUTE", state="QUEUED")
        s.add(job)
        s.commit()
    JOB_ID = job.job_id
    engine.dispose()
    step(f"临时库+EXECUTE JobRow 直造完成: {JOB_ID} (QUEUED)")

    t0 = time.time()

    # ---- 1. 起 API → HTTP claim ----
    api1 = start_api(db_url)
    if not wait_up():
        print("[FATAL] API#1 启动失败")
        api1.kill()
        return 2
    step("API#1 started (uvicorn 子进程, port 8612)")

    st, claimed = call("POST", "/jobs/claim", {"node_id": "drill-node"}, idem="drill-claim-0001")
    job_body, lease_body = (claimed or {}).get("job"), (claimed or {}).get("lease")
    if st != 200 or not job_body:
        print(f"[FATAL] claim 失败: {st} {claimed}")
        api1.kill()
        return 2
    lease_id = lease_body["lease_id"]
    token = lease_body["fencing_token"]
    step(f"HTTP claim OK: lease={lease_id[:20]}.. fencing_token={token}")

    # ---- 2. WAL：3 条事件，仅 seq1 真实送达并 ack ----
    from dsh_sim.worker.wal import JsonlWal

    wal = JsonlWal(tmpdir / "worker" / "worker.wal.jsonl")

    def ev_body(seq: int, kind: str, payload: dict):
        return {
            "lease_id": lease_id,
            "fencing_token": token,
            "event_seq": seq,
            "kind": kind,
            "payload": payload,
        }

    wal.append(job_id=JOB_ID, lease_id=lease_id, fencing_token=token,
               event_seq=1, kind="STARTING", payload={"attempt_id": "att_drill", "launch_intent_id": "intent-1", "work_dir_summary": "deadbeef", "evidence_mode": "MOCK"})
    wal.append(job_id=JOB_ID, lease_id=lease_id, fencing_token=token,
               event_seq=2, kind="RUNNING", payload={"process_identity": "mock:1234", "software_build": "mock-2402", "stage": "launch", "evidence_mode": "MOCK"})
    wal.append(job_id=JOB_ID, lease_id=lease_id, fencing_token=token,
               event_seq=3, kind="HEARTBEAT", payload={"stage": "RUNNING", "log_location": "var/worker/run.log", "resource_state": {"cpu_cores": 1}, "evidence_mode": "MOCK"})
    step("WAL append seq1 STARTING / seq2 RUNNING / seq3 HEARTBEAT")

    st, _ = call("POST", f"/jobs/{JOB_ID}/events", ev_body(1, "STARTING", {"attempt_id": "att_drill", "launch_intent_id": "intent-1", "work_dir_summary": "deadbeef", "evidence_mode": "MOCK"}), idem="drill-evt-seq1")
    assert st == 202, f"seq1 上报失败: {st}"
    wal.mark_acked(JOB_ID, 1)
    step("seq1 经 HTTP 真实送达 (202) → mark_acked(1)")

    # ---- 3. SIGKILL → 离线 append seq4 ----
    api1.kill()  # SIGKILL 等价（Windows TerminateProcess 为硬终止，无优雅关闭）
    api1.wait(timeout=10)
    assert wait_down(PORT), "API 端口仍可达，kill 未生效"
    step("API#1 SIGKILL（端口已不可达）")

    wal.append(job_id=JOB_ID, lease_id=lease_id, fencing_token=token,
               event_seq=4, kind="COMPLETED", payload={"exit_code": 0, "artifact_ids": [], "verification_id": "ver_drill", "numerical_conclusion": "PASS", "applicability": "IN_SCOPE", "evidence_mode": "MOCK"})
    un = wal.unacked()
    step(f"API 死亡期间 WAL append seq4 COMPLETED OK；unacked={[ (r['event_seq'], r['kind']) for r in un ]}")
    if [r["event_seq"] for r in un] != [2, 3, 4]:
        print("[FATAL] WAL unacked 与预期不符")
        return 2

    # 断连期直接 HTTP 上报应失败（证明真的断）
    try:
        st_down, _ = call("POST", f"/jobs/{JOB_ID}/events", ev_body(2, "RUNNING", {}))
        down_ok = False
    except Exception:
        down_ok = True
    step(f"断连期 HTTP post_event 不可达: {'是' if down_ok else '否(意外)'}")
    if not down_ok:
        FINDINGS.append("断连期 HTTP 仍可达，kill 未真正模拟断连")

    # ---- 4. 重启 API → 按序重传 ----
    api2 = start_api(db_url)
    if not wait_up():
        print("[FATAL] API#2 重启失败")
        api2.kill()
        return 2
    step("API#2 restarted")

    replayed = []
    for rec in wal.unacked():  # (job_id,event_seq) 排序 → 2,3,4
        st, resp = call("POST", f"/jobs/{JOB_ID}/events",
                        ev_body(rec["event_seq"], rec["kind"], rec["payload"]),
                        idem=f"drill-replay-seq{rec['event_seq']}")
        replayed.append((rec["event_seq"], st))
        if st == 202:
            wal.mark_acked(JOB_ID, rec["event_seq"])
    step(f"按序重传 seq2/3/4 → HTTP 状态 {replayed}")
    un2 = wal.unacked()
    step(f"重传后 WAL unacked 数 = {len(un2)}（应为 0）")

    events = read_events(str(db_path))
    step(f"events 表该 job 全量: {events}")
    ok_events = (
        [ (s, k) for s, k in events ] == [(1, "STARTING"), (2, "RUNNING"), (3, "HEARTBEAT"), (4, "COMPLETED")]
    )
    seqs = [s for s, _ in events]
    no_dup = len(seqs) == len(set(seqs))

    # ---- 5. 幂等性：seq2 故意重传第二遍 ----
    # 幂等键用新值：让请求穿透 HTTP 幂等层到达队列层，检验 (job_id,event_seq) 去重
    st_dup, resp_dup = call("POST", f"/jobs/{JOB_ID}/events", ev_body(2, "RUNNING", {"process_identity": "mock:1234", "software_build": "mock-2402", "stage": "launch", "evidence_mode": "MOCK"}), idem="drill-dup-seq2-newkey")
    events_after = read_events(str(db_path))
    dup_created = len(events_after) != len(events)
    step(f"seq2 二次重传: http={st_dup} code={resp_dup.get('code')} 新增行={'是' if dup_created else '否'}")
    if st_dup == 202:
        # 队列层 post_event 对幂等重发返回已存事件（HTTP 202）——去重不靠报错
        if dup_created:
            FINDINGS.append(f"幂等重传产生重复行: {events_after}")
        else:
            step("幂等语义: HTTP 202 + 服务端不新增行（同 kind 幂等重发返回已存事件）")
    else:
        FINDINGS.append(f"幂等重传被拒: http={st_dup} body={resp_dup}（队列层设计为同 (job,seq,kind) 返回已存事件 202；若非 409/202 语义则需核对）")

    # job 终态后（COMPLETED 已到 SUCCEEDED），seq2 是晚到事件——但这里 seq 是旧的，
    # post_event 幂等分支在状态检查之前，行为应为返回已存事件。上面已如实记录。

    api2.terminate()
    try:
        api2.wait(timeout=10)
    except subprocess.TimeoutExpired:
        api2.kill()

    elapsed = time.time() - t0
    passed = (
        ok_events and no_dup and not un2 and down_ok
        and replayed == [(2, 202), (3, 202), (4, 202)]
    )
    print()
    print("=" * 62)
    print(f"WAL 断连重传演练汇总  (耗时 {elapsed:.2f}s)")
    print("=" * 62)
    print(f"事件落表   : {len(events)} 条 {[s for s, _ in events]} 顺序正确={'是' if ok_events else '否'}")
    print(f"重复检出   : {'无' if no_dup else '有(FAIL)'}")
    print(f"重传明细   : {replayed}（应为 [(2,202),(3,202),(4,202)]）")
    print(f"重传后未确认: {len(un2)}（应为 0）")
    print(f"FINDING    : {len(FINDINGS)} 条")
    for f_ in FINDINGS:
        print(f"  - {f_}")
    print("=" * 62)
    print(f"结论: {'PASS' if passed else 'FAIL'}")
    print(f"  db: {db_path}")
    print(f"  wal: {tmpdir / 'worker' / 'worker.wal.jsonl'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
