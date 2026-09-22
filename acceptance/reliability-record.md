# 可靠性实测记录（reliability-record）

> 一次性可靠性验证任务产物（2026-09-22）。两项证据此前只在单测层验证过，本记录为
> **真实验证**：真实双进程 / 真实 uvicorn 子进程 / 真实 SIGKILL / 真实 SQLite 文件库。
> 依据 CONVENTIONS §0 诚实红线：没跑就是 NOT_RUN，输出如实摘录，不美化。

## 环境

- OS：Windows 11 Home China；Python 3.13.14（managed venv）
- 解释器：`<PYTHON_ENV>\Scripts\python.exe`
- 工程 API 8600 / dsh 3080 均未运行（脚本自起临时实例，端口 8612）
- 被测代码：`src/dsh_sim/queue/service.py`（claim/post_event）、
  `src/dsh_sim/worker/wal.py`（JsonlWal）、`src/dsh_sim/db/session.py`（BEGIN IMMEDIATE）

---

## R1 · 双进程并发 claim 压测（stress_concurrent_claim.py）

- **场景**：同一 SQLite 文件库（临时目录，WAL 模式），主进程入队 N 个 PREPARE 作业，
  spawn 2 个独立子进程（各自独立 engine/SessionFactory，engine 上挂与工程
  `db/session.py` 相同的 BEGIN IMMEDIATE begin 事件），并发循环 `queue.service.claim`
  直到队空。子进程先经 raw sqlite3 upsert `NodeRow.max_concurrent=1000` 放开节点配额
  （配额非本压测目标），其余全部走真实 claim 路径。
- **命令**：
  `python.exe scripts/stress_concurrent_claim.py 20` 与 `... 50`
- **关键输出（N=20，2026-09-22，耗时 1.61s）**：
  ```
  node-1 领取  17 | node-2 领取   3 | 合计 20
  重复分配   : 0 (OK)
  遗漏       : 0 (OK)
  多活跃租约 : 0 (OK)
  fencing_token 每 job 不重复: OK (active=20, 首租 token 全为 1: 是)
  fencing_counter==token（job 级单调自 1）: OK
  重派验证   : 3/3 作业二次出租 token=2 (OK)
  锁等待重试 : 合计=0
  子进程错误 : 无
  结论: PASS
  ```
- **关键输出（N=50，复跑轮，耗时 1.96s）**：node-1 33 / node-2 17，合计 50；
  重复 0 / 遗漏 0 / 多活跃租约 0 / token 每 job 唯一 / 重派 3/3 token=2 /
  锁重试 0 / 结论 PASS。
- **结论**：**PASS**（N=20 与 N=50 两轮；断言 1-4 全部真实验证）
- **执行时间**：2026-09-22（N=20 两轮 + N=50 三轮，含装置调试）
- **验证方式说明**：
  - 断言 1/2 汇总两子进程领取结果查重、与入队清单比对；
  - 断言 3 直接读 leases/jobs 表核对 active 计数、fencing_counter；
  - 断言 4 子进程对 OperationalError 含 "database is locked" 计数重试
    （busy_timeout=5s 生效后本轮 0 次触发，即并发争锁被驱动层等待吸收，未冒错）；
  - 追加"重派"白盒段：3 个已领作业重置 QUEUED 后再 claim，token 1→2。
- **环境补丁（FINDING-2，见下）**：`db/session.py make_engine` 的 connect_args 只配
  `check_same_thread`，未配 timeout 与 journal_mode。脚本内对每个连接显式
  `PRAGMA journal_mode=WAL + busy_timeout=5000`。

## R2 · Worker WAL 真断连重传演练（drill_wal_reconnect.py）

- **场景**：临时库起 uvicorn API 子进程（8612）→ 直造 EXECUTE JobRow →
  NODE_ADMIN 经 HTTP `/jobs/claim` 真实领取 → Worker 侧 JsonlWal append
  seq1 STARTING / seq2 RUNNING / seq3 HEARTBEAT，seq1 经 HTTP 真实送达后 mark_acked →
  **SIGKILL 杀 API**（kill 后轮询端口确认不可达）→ 离线 append seq4 COMPLETED →
  重启 API → `unacked()` 按序重传 seq2/3/4 → 核对 events 表 → 幂等性二次重传 seq2。
- **命令**：`python.exe scripts/drill_wal_reconnect.py`
- **关键输出（2026-09-22，耗时 5.60s）**：
  ```
  HTTP claim OK: lease=lease_a429484686914c.. fencing_token=1
  seq1 经 HTTP 真实送达 (202) → mark_acked(1)
  API#1 SIGKILL（端口已不可达）
  API 死亡期间 WAL append seq4 COMPLETED OK；unacked=[(2,'RUNNING'),(3,'HEARTBEAT'),(4,'COMPLETED')]
  断连期 HTTP post_event 不可达: 是
  API#2 restarted
  按序重传 seq2/3/4 → HTTP 状态 [(2, 202), (3, 202), (4, 202)]
  重传后 WAL unacked 数 = 0
  events 表该 job 全量: [(1,'STARTING'),(2,'RUNNING'),(3,'HEARTBEAT'),(4,'COMPLETED')]
  seq2 二次重传: http=202 新增行=否
  结论: PASS
  ```
- **结论**：**PASS**（不丢：4/4 落表；不重：seq 唯一；有序：1→4；WAL 断连期可写；
  重传后 unacked 清零；幂等重传服务端不新增行）
- **执行时间**：2026-09-22
- **说明**：EXECUTE 链按演练预案降级为"直造 JobRow + 真实 HTTP claim"（完整
  prepare→authorize→submit 链搭建成本高，且本演练目标是 WAL/重传语义而非链路）。
  事件载荷字段按 loop.py 的最低载荷要求构造，evidence_mode 均为 "MOCK"。

---

## FINDING 清单

- **FINDING-1（口径澄清，非缺陷）**：fencing_token 为 **job 级单调**
  （token = job.fencing_counter，首租均为 1，跨 job 可同值），不是全局唯一序号。
  数据库唯一约束在 `(job_id, event_seq)` 与 `lease_id` 主键上，跨 job token 无唯一
  约束。需求文字"fencing_token 全局单调递增不重复"若按字面全局口径理解，与实现
  不一致；按分布式 fencing 语义（每 job 内单调、旧租约 token 必小于新租约）实现
  是自洽的。本压测按 job 级口径断言通过，并附重派 token 1→2 递增证据。
- **FINDING-2（工程侧改进建议，未改服务代码）**：`db/session.py make_engine`
  对 SQLite 未配置 `connect_args={"timeout": ...}`，也未设 `journal_mode=WAL`。
  本任务两脚本均在测试装置内自行补齐（每连接 PRAGMA WAL + busy_timeout=5000）。
  若工程部署直接沿用 make_engine 而无外层补丁，并发写场景可能出现
  "database is locked" 立即报错（默认 busy_timeout=0）。建议工程侧评估补齐。
- **FINDING-3（幂等行为实测，与设计一致）**：队列层 `post_event` 对同
  `(job_id, event_seq, kind)` 的重发返回**已存事件（HTTP 202）且不新增行**——
  去重不靠报错。实测 seq2 以新 Idempotency-Key 二次重传：HTTP 202、events 表无
  新行，与 service.py docstring"重复序号视为幂等重发"一致，无出入。
  （注意：同 kind 才幂等；同 seq 异 kind 会被 409 拒绝，本次未触发该分支。）

## 产物清单

- `scripts/stress_concurrent_claim.py`（R1，可重复执行，N 可调）
- `scripts/drill_wal_reconnect.py`（R2，可重复执行）
- `acceptance/reliability-record.md`（本文件）
