# dsh-sim 功能覆盖深度清点报告

> 清点时间：2026-09-21 15:33–16:10 · 方法：全部实测（测试运行、覆盖率测量、HTTP 复测、SIGKILL 演练），无一项来自文档声称。
> 附带产出：**3 个真实缺陷当场修复 + P08/P09 探针从 BLOCKED 升级为 PASS**。

## 一、功能覆盖总览（实测）

| 层 | 覆盖现状 | 证据 |
|---|---|---|
| 契约 | ✅ canonical-json(23 向量) + draft/spec Schema + OpenAPI 0.1.1 | test_canonical 6 用例 |
| 领域/状态机 | ✅ 五维状态 + 全迁移矩阵 + 唯一约束 | test_domain 19 用例 |
| 持久层 | ✅ 22 表全约束 + 幂等 + **本轮修复断电竞态** | test_db 11 用例 |
| 队列 | ✅ 原子 claim/fencing/租约过期/晚到事件 | test_queue 14 用例 |
| API | ✅ 全 operationId + 统一错误模型 + 项目隔离 | test_api_contract 24 用例 |
| Mock 执行链 | ✅ 六 Run 端到端 + Worker 心跳/WAL | test_mock_chain 4 用例 |
| 校核/证据 | ✅ 独立复算 + 冻结 Bundle + 缺值不补 0 | test_verify 15 / test_evidence 8 |
| 审查闭环 | ✅ ACCEPT 六类阻塞逐条拒 + STALE 历史保留 | test_review_flow 4 用例 |
| MCP 12 工具 | ✅ 全实现（dsh-sim venv 147 passed 含 11 条工具测试） | test_mcp_tools |
| 能力包注册 | ✅ 扫描→DRAFT 注册→RELEASED 不可自动 | test_capabilities_registry 4 |
| 双面板 | ✅ 执行台/审查台 + 云图内联（真实 API 通道） | curl 实测 200 |
| DSH 插件 | ✅ 右侧栏页签+设置卡+前缀代理+健康探针 | /dsh-sim/health upstream_ok:true |

**测试基线**：default env **136 passed + 1 skipped**；dsh-sim venv **147 passed**；覆盖率 **86%**（3375 语句 / 467 未覆盖）。

## 二、探针现状（P01–P09）

| 探针 | 状态 | 说明 |
|---|---|---|
| P01 版本/授权 | PASS | STAR-CCM+ 2402 R8 实测 |
| P02 模板打开/保存 | PASS | 限定 |
| P03 参数写入/回读 | PASS | 限定 |
| P04 角色绑定 | NOT_RUN | 依赖批准模板登记 |
| P05 真实求解/导出 | PASS | 续算限定（ccmpsuite_solve 可用） |
| P06 重复性 | NOT_RUN | 依赖 TBD-06 容差冻结 |
| P07 取消/清理 | PASS | 串行 batch 限定 |
| **P08 断线重入** | **PASS（工程服务层限定）← 本轮升级** | SIGKILL 后任务+作业存活、重启可 claim |
| **P09 权限隔离** | **PASS（开发身份模式限定）← 本轮升级** | AGENT 授权/跨项目/职责分离三门全 403 |

## 三、本轮发现并修复的缺陷（全部有复测证据）

### 缺陷 1：MCP 测试在 default env 收集崩溃
fastmcp 迁独立 venv 后，共享 env 下 `ModuleNotFoundError` 导致**全量测试中断**。
修复：`pytest.importorskip("fastmcp")` 优雅降级 → default env 跳过 1 条、venv 全跑 11 条，两边全绿。

### 缺陷 2：持久化竞态（P08 复测抓到的真缺陷，严重度最高）
**现象**：`prepareTask` 返回 202 + job_id，但 SIGKILL 后库里 preparation/jobs 全空。
**根因**：FastAPI yield 依赖的 `session.commit()` 在**响应已发送之后**才执行——客户端拿到作业号但持久队列里没有该作业，崩溃窗口内"持久化承诺"是空头支票。
**修复**：`api/deps.py` 的 `IdempotencyContext.store()` 内同步 commit（业务行+幂等记录同事务、响应前落盘；get_session 的后续 commit 为幂等 no-op）。
**复测**：`state=PREPARING` 存活 + `claim → job_5f35... kind=PREPARE` ✅；147 测试无回归。

### 缺陷 3：探针记录失真
P08/P09 的 BLOCKED 理由（"队列/Worker/身份未交付"）早已过时，与系统真实状态脱节。
修复：写 `scripts/reprobe_p08_p09.py` 真实复测并更新两份探针 JSON + probe-report.md。

## 四、优化空间（按杠杆排序）

| # | 优化项 | 杠杆 | 现状与差距 |
|---|---|---|---|
| 1 | **monitor 面板：求解中滚动残差迷你图** | 高 | 事件流+after_seq 增量端点已有，面板 4–6s 轮询已有，只缺前端 sparkline 渲染 |
| 2 | 真实执行链 7 个 NotImplementedError | 高 | prepare_case/launch/poll/cancel/collect/extract/readback——每条解锁条件已写在异常消息里（WP-06 固定宏、TBD-07 license、TBD-06 口径） |
| 3 | mcp/server.py 覆盖率 0%（default env 视角） | 中 | 工具逻辑在 venv 有 11 条测试，但覆盖率统计不含；可将 stdio_e2e_check.py 纳入常规门禁 |
| 4 | ~~evidence/report.py 仅 14 行骨架~~ **误判更正（09-22）** | — | 覆盖表"14"是语句数（0 miss，100% 覆盖）；render_report 已接入 build_bundle 且有 3 条专项测试（水印/章节/确定性渲染）全绿 |
| 5 | SQLite 并发 claim 只测过逻辑层 | 中 | BEGIN IMMEDIATE 语义正确，但未做双进程并发压测（claim 原子性的最后一公里） |
| 6 | 41 条 MOCK TC 未回填 status | 中 | test_matrix.csv 全 NOT_EXECUTED，与 136 个已绿测试脱钩——验收追踪失真 |
| 7 | 云图渲染质量 | 中 | batch contour 部分单色（场景未绑定 displayable）；需宏内显式建 Part→Scene→displayer |
| 8 | 面板轮询 → SSE/长轮询 | 低 | after_seq 增量事件可替换 4–6s 轮询，降服务压力 |
| 9 | Worker WAL 真断连演练 | 低 | 单测覆盖了重传逻辑，未在真实断网/杀进程场景演练 |

## 五、诚实边界（不因清点而变化）

- FR-32 仍全 DESIGNED：验收观察未走完前不标"已实现"（与 TC 回填是同一件事的两面）
- P04/P06 NOT_RUN、生产 IdP（TBD-08）、能力包阈值冻结（TBD-05/06）——均需 Owner/管理员动作，系统侧就绪
- 插件 React 客户端的浏览器视觉验收仍为 NOT_RUN（服务端全部实证通过）
