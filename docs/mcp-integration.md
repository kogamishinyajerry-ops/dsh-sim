# dsh-sim MCP 接入说明（WP-11 / Agent G）

> 状态：IMPLEMENTED（12 工具 + dsh web profile 配置段）；
> dsh 侧 ListTools 端到端验证 **NOT_RUN**（需重启 dsh web，见 §6 checklist）。
> 依据：定义书 §DSH接入与工具边界 / §锁版接入方式；CONVENTIONS §3.5。

## 1. 工具 ↔ API 映射表

12 个工具名与 CONVENTIONS §3.5 一字不差。全部经 httpx 调工程 API
（base URL 取 env `DSH_SIM_API_URL`，默认 `http://127.0.0.1:8600/api/v1`），
单次调用超时 60s（定义书 §锁版接入方式）。

| MCP 工具 | API | 说明 |
|---|---|---|
| `list_capabilities` | `GET /capabilities` | 已发布（RELEASED）能力包，游标分页 |
| `get_task` | `GET /tasks/{task_id}` | ⚠ 服务路由缺口，见 §7 |
| `create_task` | `POST /tasks` | 草稿；缺输入以 open_questions 显式阻塞 |
| `revise_task` | `POST /tasks/{task_id}/revisions` | expected_revision 防并发覆盖 |
| `prepare_task` | `POST /tasks/{task_id}/prepare` | 202 + preparation_id/job_id + 轮询提示 |
| `get_preparation` | `GET /preparations/{preparation_id}` | 真实回读与差异；READY≠工程通过 |
| `submit_runs` | `POST /tasks/{task_id}/submissions` | 需已存在人工授权；202 + run_ids |
| `get_run` | `GET /runs/{run_id}?after_seq=` | 多维状态 + 增量事件 |
| `cancel_run` | `POST /runs/{run_id}/cancel` | 取消请求≠已停止 |
| `build_bundle` | `POST /tasks/{task_id}/bundles` | 202 + bundle_id/job_id（当前服务 503 BLOCKED，Agent E） |
| `get_evidence` | `GET /bundles/{bundle_id}` | 摘要 + artifact 引用；manifest 截断（默认 20 条 + truncated 标记） |
| `draft_review_issue` | `POST /reviews/{review_id}/issues` | 永远 DRAFT；转 OPEN 需审查人 confirmIssue |

长操作（prepare/submit/build_bundle）快速返回资源 id + `hint` 轮询提示；
**不把大文件/场数据塞进文本返回**（get_evidence 只给引用，内容经受权
artifact 接口另行流式读取）。

## 2. 身份边界（AGENT 无授权/接受能力）

- 工具一律以 AGENT 身份调 API：`X-Dev-Subject: agent-dsh-sim` / `X-Dev-Roles: AGENT` /
  `X-Dev-Projects: $DSH_SIM_AGENT_PROJECTS`（默认 `proj_a`，开发模式；生产身份体系 TBD-08）。
- 工具清单中**根本不存在** authorizeRuns / decideReview / confirmIssue / closeIssue；
  即使绕过 MCP 直接调 API，服务端对 Agent Bearer 也始终 403（`identity.require_human()`，
  见 `src/dsh_sim/api/services/run_service.py`、`review_service.py`）。
- 每个工具 docstring 均写明"模型侧无授权/接受能力"。
- 禁止暴露 run_any_code / 任意 SQL / 任意文件路径 / approve 类工具：
  server 启动时自检工具清单（数量、名称、禁止模式扫描），打印到 stderr
  （stdout 是 JSON-RPC 通道），自检失败 `SystemExit(2)`。

## 3. 错误模型

- 服务端 Error 模型 `{code,message,retryable,trace_id,details}` 原样透传（含 403/409/BLOCKED）。
- 服务不可达（ConnectError/Timeout/NetworkError）→ 桥层本地生成：
  `{code:"UNAVAILABLE", message:"工程服务不可用", retryable:true,
  trace_id:<本地 uuid>, details:{source:"mcp-bridge", reason:<异常类型>}}`。
- 非错误模型的异常响应 → `{code:"HTTP_<status>", ...}`。
- **禁止 fallback 到本地生成脚本或伪造数据**（定义书 §锁版接入方式：
  连接不上必须显示"工程服务不可用"）。

## 4. 超时、幂等与轮询模式

- 单次调用 60s（`httpx.Timeout(60.0)`）；dsh 侧 `toolCallTimeoutMs: 120000` 留余量。
- 副作用 POST 的 `Idempotency-Key`：显式 `idempotency_key` 参数优先；否则按
  `工具名 + canonical_dumps(参数)` 确定性派生（`mcp-<sha256[:48]>`）——
  模型同参数重试天然命中服务端幂等记录，返回原对象（CONVENTIONS §3.4）。
- 长作业轮询：`prepare_task` → `get_preparation(preparation_id)`；
  `submit_runs` → `get_run(run_id)`；`build_bundle` → `get_evidence(bundle_id)`。
  建议 2—5s 间隔带抖动，不忙等（定义书 §并发、断线、取消与补算规则）。

## 5. dsh 侧配置段（已追加）

`<DSH_HOME>\home\profiles\web\cordis.patch.yml`（改前已备份
`cordis.patch.yml.bak-before-dsh-sim`），追加于文件末尾，不动既有任何行：

```yaml
- insert:
    - id: mcp-dsh-sim
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: dsh-sim
        transport: stdio
        command: '<PYTHON_ENV>\Scripts\python.exe'
        args: ['-m', 'dsh_sim.mcp.server']
        env:
          PYTHONIOENCODING: utf-8
          DSH_SIM_API_URL: 'http://127.0.0.1:8600/api/v1'
        toolCallTimeoutMs: 120000
        failOnStartupError: false
        reconnect:
          enabled: true
```

说明：dsh-sim 已 `pip install -e .` 进该 managed env（0.1.0），故无需 PYTHONPATH。
`failOnStartupError: false` 保证工程服务未起时 dsh 照常启动，工具调用时返回
UNAVAILABLE。生效后工具名为 `mcp__dsh-sim__<tool>`。

## 6. 重启 dsh web 后的验证 checklist（给主会话/用户）

1. 起工程服务：`.../Scripts/uvicorn.exe dsh_sim.api.main:app --port 8600`
   （工作目录 `dsh-sim/`）。
2. 重启 dsh web（或等 cordis 配置热载）。
3. 会话内确认 12 个工具出现：`mcp__dsh-sim__list_capabilities` …
   `mcp__dsh-sim__draft_review_issue`（与 §1 左列一一对应）。
4. 调 `mcp__dsh-sim__list_capabilities`：服务在 → 返回 `{items, next_cursor}`；
   停服务再调 → 返回 `{code:"UNAVAILABLE", retryable:true}`（不得出现伪造数据）。
5. 验证身份边界：模型无法找到/调用任何 authorize/decide/confirm/approve 类工具；
   `draft_review_issue` 创建的问题状态为 DRAFT。
6. 若工具不出现：查 dsh web 日志中 `mcp-dsh-sim` 段加载与 server stderr
   （自检输出以 `[dsh-sim-mcp]` 开头）。

## 7. 已知缺口（如实标注）

- **get_task 服务路由缺口**：`GET /tasks/{task_id}` 未在 `src/dsh_sim/api/routes/`
  暴露（api/ 属 Agent C/E 边界，本 WP 不改）。MCP 工具按契约意图实现；对当前真实
  服务调用会收到 404（翻译为 `HTTP_404` 结构化错误）。请 Agent C/E 补挂该只读
  端点（OpenAPI 契约层同样缺 getTask，需 WP-23 对齐）。测试中以显式补挂路由的
  测试 app 验证 MCP 投影行为。
- **build_bundle**：服务端显式 503 `BLOCKED`（`review_service.build_bundle`，
  evidence/ 冻结链属 Agent E / WP-13）。MCP 工具如实透传，不补造 manifest。
- **get_evidence**：依赖 Bundle 存在；证据链交付前只能对测试构造的 Bundle 查询。
