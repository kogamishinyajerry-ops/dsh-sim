# dsh-sim · 跨 Agent 开发契约（CONVENTIONS）

> 所有参与本项目的编码 Agent **必须先读本文件**，再动手。本文件是唯一权威约定，冲突以它为准。
> 项目依据：`DSH工业仿真智能体_首版开发设计定义书_v0.1.md`（下称"定义书"）。

## 0. 诚实红线（最高优先级，违反即返工）

1. **MOCK 绝不冒充 REAL**：所有 Mock 产生的数据/状态/报告，字段 `evidence_mode: "MOCK"` 必带；UI、报告、API 响应中必须肉眼可区分。
2. **没有真实证据就标 NOT_RUN**：探针、真实求解器测试，没跑就是 `NOT_RUN`，禁止写"预计通过"、禁止补造 `.log/.sim/.csv`。
3. **工程阈值一律 `null`/`"TBD"`**：质量不平衡容差、监控窗口、网格要求、参考容差、参数上下界——这些是 Owner 冻结项，**不是开发能填的**。填了就是缺陷。
4. **"已设计" ≠ "已实现"**：文档、CSV 追踪矩阵中状态列如实填写（DESIGNED / IMPLEMENTED / TESTED / NOT_RUN / BLOCKED）。
5. **失败不删除**：测试失败、探针失败如实记录，不删用例、不降容差、不改状态机让整单变绿。

## 1. 技术栈（冻结）

- **Python**：3.13（managed），统一解释器：
  `<USER_HOME>\.workbuddy\binaries\python\versions\3.13.12\python.exe`
- **包管理**：统一 venv `<USER_HOME>\.workbuddy\binaries\python\envs\default`
  - 安装：`<USER_HOME>\.workbuddy\binaries\python\envs\default\Scripts\pip.exe install <pkg>`
  - 已装（编排层预置）：fastapi, uvicorn, sqlalchemy, pydantic v2, jinja2, pytest, httpx, fastmcp, jsonschema
  - **新增依赖前必须检查离线可用性**；禁加 LangGraph/CrewAI/Airflow/K8s（定义书明令）
- **单包结构**：`src/dsh_sim/` 一个 Python 包，子模块见 §2。禁止再造平行包。
- **代码风格**：纯函数模块优先于 OOP；dataclass + type hints；`from __future__ import annotations`；禁止隐式全局状态（DB session 走显式工厂）。
- **DB**：SQLAlchemy 2.x，方言 PG 兼容；开发库 `sqlite:///<repo>/var/dsh_sim.db`（URL 从 `DSH_SIM_DATABASE_URL` 读）。禁用 SQLite-only 语法；JSON 列用 `sqlalchemy.JSON`。
- **前端**：panels 为 FastAPI 静态托管的原生 ES Module 页面（无框架、无构建步骤）；配色用 DSH 设计令牌 `--dsw-alias-*`（参考 dsh-visual-plugin 0.3.2 的 CSS 变量集），浅色主题优先。
- **TS/DSH 插件**：本轮不做原生 sidebar 槽位插件（定义书 §锁版接入方式允许的"受保护审查页"降级实现，状态墙中注明期限）。

## 2. 目录与模块边界

```
dsh-sim/
  contracts/          ← Agent A：OpenAPI + JSON Schema + canonical 规范（本层只产契约，不写实现）
  src/dsh_sim/
    canonical/        ← 编排层已交付：canonical-json-v1 参考实现 + 哈希（冻结，别人只 import 不改）
    domain/           ← Agent C：Pydantic 模型、五维状态机、枚举
    db/               ← Agent C：SQLAlchemy models/session/迁移
    queue/            ← Agent C：Job 队列、租约、fencing token、事件
    api/              ← Agent C→E：FastAPI 路由（薄层，业务在 services）
    adapters/         ← Agent D/E：STAR 适配器接口 + Mock + CLI 桥实现
    worker/           ← Agent E：Worker 领取循环
    verify/           ← Agent E：指标提取 + 独立 Verifier
    evidence/         ← Agent E：Bundle 冻结 + Jinja 报告
    review/           ← Agent E：Issue/Decision 闭环
    mcp/              ← Agent G：FastMCP 12 工具
  panels/             ← Agent F：executor/ reviewer/ shared/（纯静态，fetch /api/v1/...）
  capabilities/       ← Agent B：buffer_chamber/0.1.0 全结构
  acceptance/         ← Agent B：requirements.csv + test_matrix.csv；probes/ 由 Agent D 填
  compatibility/      ← Agent D：P01-P09 探针记录
  tests/              ← 各 Agent 自测代码放这；markers：mock / real_solver / integration
  var/                ← 运行期产物（db、artifact 存储），.gitignore
  README.md           ← 编排层维护：诚实状态墙
```

**边界纪律**：你的 PR 只能写你名下目录 + `tests/`。跨模块调用只走已冻结接口（canonical、domain 模型、api 路由签名）。要改别人目录 → 在交接说明里写 BLOCKED 请求，不要直接改。

## 3. 关键冻结接口

### 3.1 canonical / 哈希（编排层已交付，`src/dsh_sim/canonical/`）

```python
from dsh_sim.canonical import canonical_dumps, sha256_hex, spec_sha256, prepared_digest

canonical_dumps(obj) -> str          # NFC、键排序、UTF-8、拒绝 NaN/Infinity、无多余空白
sha256_hex(s: str) -> str
spec_sha256(task_spec_dict) -> str   # 完整规范化业务输入的哈希
prepared_digest(spec_sha, artifacts: dict[str,str], readback_sha: str, adapter_build: str) -> str
```

- 数组顺序有意义，**禁止排序数组**。
- 哈希只能由服务侧计算；时间戳、会话文本、展示格式不进摘要。

### 3.2 五维状态枚举（定义书 §任务、数值、适用与审查状态，Agent C 在 `domain/states.py` 实现）

- TaskFlow: `DRAFT → PREPARING → READY → AUTHORIZED → ACTIVE → READY_FOR_REVIEW → IN_REVIEW` → 终态 `ACCEPTED / CHANGES_REQUESTED / REJECTED`
- Execution: `QUEUED, WAITING_RESOURCE, LEASED, STARTING, RUNNING, CANCELLING, COLLECTING, SUCCEEDED, FAILED, CANCELLED, LOST`
- Numerical: `NOT_CHECKED / PASS / FAIL / INSUFFICIENT`（程序成功 ≠ PASS）
- Applicability: `IN_SCOPE / OUT_OF_SCOPE / UNCONFIRMED`
- Review: `NOT_SUBMITTED / PENDING / CHANGES_REQUESTED / ACCEPTED / REJECTED`
- Validity: `CURRENT / STALE`

### 3.3 错误模型（所有 API 统一）

```json
{ "code": "STRING_ENUM", "message": "...", "retryable": false, "trace_id": "uuid", "details": {} }
```

HTTP 映射：401 未认证 / 403 越权 / 409 摘要·修订·幂等冲突 / 422 工程输入不成立 / 429 配额 / 503 暂不可用。

### 3.4 幂等

产生副作用的 POST 必带 `Idempotency-Key` 头；约束 = 主体+项目+action+key 唯一；同 key 同摘要返回原对象，同 key 异摘要 → 409。

### 3.5 12 个 MCP 工具名（Agent G 实现，**一字不差**）

`list_capabilities, get_task, create_task, revise_task, prepare_task, get_preparation, submit_runs, get_run, cancel_run, build_bundle, get_evidence, draft_review_issue`

**禁止**暴露：run_any_code / 任意 SQL / 任意文件路径 / approve 类工具。

## 4. 测试纪律

- `pytest -m mock`：默认全绿线，必须不需要 STAR-CCM+、不需要网络。
- `pytest -m real_solver`：需要真实 STAR-CCM+；没跑就 NOT_RUN 记录，不准 skip 后自称通过。
- 每个完成声明必须附：变更文件清单、对应 FR/TC、执行命令、实际输出（截关键行）、真实/模拟模式、已知缺口。
- coverage 目标 ≥80（核心 domain/queue/verify 模块）。

## 5. 命名与编号

- FR 编号 FR-01..32、TC 编号 TC-001..064、P 探针 P01..P09、WP 编号 WP-01..24 —— 全部沿用定义书，禁止自编新号。
- Git 提交信息：`<WP-xx|FR-xx> 简述`。
- 文件内引用定义书条款用 `§节名` 格式。

## 6. 运行入口（Agent 联调用）

```bash
# 安装/同步依赖
<USER_HOME>\.workbuddy\binaries\python\envs\default\Scripts\pip.exe install -e .
# 起工程服务（开发库 SQLite）
# 身份模式 fail-closed：本地联调必须显式声明开发模式，否则 X-Dev-* 头一律 401
# PowerShell: $env:DSH_SIM_IDENTITY_MODE="dev"  /  bash: export DSH_SIM_IDENTITY_MODE=dev
<USER_HOME>\.workbuddy\binaries\python\envs\default\Scripts\uvicorn.exe dsh_sim.api.main:app --port 8600
# 起 MCP server（stdio，供 dsh 接入）
<PYTHON_ENV>\Scripts\python.exe -m dsh_sim.mcp.server
# 测试
<PYTHON_ENV>\Scripts\python.exe -m pytest tests -m mock -q
```

端口约定：工程 API **8600**（避开 dsh 3080）；panels 由 API 静态挂载 `/panels/executor`、`/panels/reviewer`。
