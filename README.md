# dsh-sim — DSH 工业仿真智能体（R0 首版）

![tests](https://img.shields.io/badge/tests-150%20passed-brightgreen) ![coverage](https://img.shields.io/badge/coverage-86%25-green) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

> 面向 STAR-CCM+ 等 CAE 求解器的**可信仿真智能体工程服务**：五维状态机 + 人工审批门 + 证据链哈希冻结 + MCP 工具接口 + 双工作台 UI。核心设计原则——**智能体永远拿不到授权与接受权**；MOCK/REAL 数据严格分色；未知状态醒目标注，拒绝"单绿勾"。
>
> 多 Agent 协同构建（编排层 + 契约/能力包/核心/探针/执行链/面板/接入七路）。**本状态墙只写真话**：IMPLEMENTED=有代码有测试；MOCK=协议跑通但数据非真实求解；NOT_RUN=未执行；BLOCKED=有明确外部依赖。

## 一句话现状

工程服务 + 12 MCP 工具 + 双工作台 + Mock 端到端闭环**已实现并 147 项测试全绿**；真实 STAR-CCM+ 探针 5/9 通过（限定）；**任何"工程通过/接受"当前都不可达成**——能力包为 DRAFT（阈值 TBD），这是设计使然，不是缺陷。

## 运行

```bash
# 工程 API（含双面板静态托管）
cd dsh-sim
<PYTHON_ENV>\Scripts\python.exe -m uvicorn dsh_sim.api.main:app --port 8600
# 执行台  http://127.0.0.1:8600/panels/executor/index.html
# 审查台  http://127.0.0.1:8600/panels/reviewer/index.html
# MCP server（dsh web 已接入，见 docs/mcp-integration.md）
# 注意：必须用独立 venv（fastmcp 4.x 强制 mcp>=2，与共享 env 的 mcp 1.x 服务互斥）
<VENV_DSH_SIM>\Scripts\python.exe -m dsh_sim.mcp.server
# 测试（默认排除 real_solver）
<PYTHON_ENV>\Scripts\python.exe -m pytest tests -m mock -q
# 自检：python scripts/selfcheck.py
```

开发模式身份头：`X-Dev-Subject` / `X-Dev-Roles`（EXECUTOR/REVIEWER/CAPABILITY_OWNER/NODE_ADMIN/AGENT）——生产必须换受信 IdP（TBD-08）。

## 状态墙

### 已实现 + 已测试（Mock 层）

| 模块 | 内容 | 测试 |
|---|---|---|
| canonical-json-v1 | NFC/键排序/拒 NaN/数组保序；spec_sha256/prepared_digest | 23 向量 + 拒绝例 |
| 五维状态机 | TaskFlow/Execution/Numerical/Applicability/Review + 迁移守卫 | 全迁移矩阵 |
| 持久层 | 定义书全部 UNIQUE 约束、乐观锁、不可变守卫（修订/Bundle/Decision） | 集成实测触发 |
| 队列/租约 | 原子 claim、fencing token、心跳 15s/租约 90s/失联→LOST、晚到事件不覆盖 CANCELLED | 并发/过期/晚到 |
| 幂等 | 主体+项目+action+key；同 key 同摘要返回原对象，异摘要 409 | 实测 |
| 权限分离 | Agent 调 authorizeRuns/decideReview/confirmIssue/closeIssue 恒 403；执行者≠审查人 | 实测拒绝 |
| Mock 执行链 | MockStarAdapter（evidence_mode=MOCK 烙印、`.mock.` 文件名）+ Worker 领取循环 + WAL 断线重传 | A/B×3 六 Run 端到端 |
| 数值校核 | 确定性提取（缺值=null+MISSING）；独立 Verifier；**阈值 TBD→UNCONFIRMED 不编造** | 提取/复算/负例 |
| 证据包/报告 | buildBundle 冻结清单（长度+sha256）；缺工况→incomplete 阻 ACCEPT；Jinja 报告 MOCK 斜纹水印、Claim 绑 artifact | 冻结/缺项/负例 |
| 审查闭环 | Issue 五段式、Agent 仅 DRAFT、关闭需审查人+证据；ACCEPT 六类阻塞逐条列因；新修订→旧包 STALE、历史保留 | 三条负例实测 |
| 能力包注册 | 启动扫描注册（仅 DRAFT）；无人工审批的 RELEASED 自动降级；幂等 | 4 项 |
| MCP 12 工具 | list_capabilities/get_task/create_task/revise_task/prepare_task/get_preparation/submit_runs/get_run/cancel_run/build_bundle/get_evidence/draft_review_issue；AGENT 身份、UNAVAILABLE 结构化错误、无禁止工具 | list_tools 实跑 + happy path |
| 双工作台 | 执行台五区域（筛选/矩阵/回读差异/运行心跳/结果先行）；审查台五区域（阻塞置顶/证据抽查链/整改/双主动作+一次性确认）；MOCK 琥珀徽章、未知态条纹、离线横幅 | JS 语法+静态引用校验 |

**测试总数：147 passed / 0 failed**（`pytest -m mock`，86s）。

### 真实验证（REAL）

| 探针 | 状态 | 证据 |
|---|---|---|
| P01 版本/授权 | **PASS** | STAR-CCM+ 2402 Build 19.02.009 实测（compatibility/P01）；license `ccmpsuite` 可 checkout，`ccmpsuite_init` 缺失 |
| P02 模板打开/保存 | **PASS** | 真实打开+另存，原件 sha256 不变 |
| P03 参数写入/回读 | **PASS** | z_min 速度 30→31 回读一致、持久化一致 |
| P05 真算与导出 | **PASS（限定）** | 已初始化案例续算 5005→5030，7 残差 CSV 导出 |
| P07 取消清理 | **PASS（限定）** | taskkill /T 进程树全终止；串行场景，MPI 未测 |

### NOT_RUN / BLOCKED（诚实清单）

| 项 | 状态 | 解锁条件 |
|---|---|---|
| P04 角色绑定 | NOT_RUN | 批准 boundary-map（TBD-05）+ 面积/质心签名录宏 |
| P06 重复性 | NOT_RUN | 容差为 Owner 冻结项（TBD-06） |
| P08 断线重入（真实） | BLOCKED | 需真实 Worker + 服务联调演练（Mock 层已测 WAL 重传） |
| P09 权限隔离（多人） | BLOCKED | 受信 IdP（TBD-08）；当前 X-Dev-* 仅开发模式 |
| STAR 生产适配器 | 部分实现 | cli_adapter 薄封装完成；prepare/launch 等依赖批准模板与 license `ccmpsuite_init` |
| UI 视觉验收 | NOT_RUN | 无头浏览器渲染未执行（静态/语法校验已过） |
| ~~dsh 侧 ListTools~~ | **PASS** | 2026-09-21 实跑验证：桥接日志 12 工具自检+FastMCP banner 启动、零报错；stdio 直连 ListTools 12/12 + CallTool 真实往返（tests/stdio_e2e_check.py） |
| Gold Case / 未见任务 / UAT | NOT_RUN | 需 Owner 投入与真实环境（TBD-10） |
| 现场验收脚本 12 步 | 部分 MOCK | 见 acceptance/field-acceptance-record.md |

### 设计性不可达成（非缺陷）

- **ACCEPT 当前必然失败**：能力包 DRAFT（阈值全 TBD）→ 范围 UNCONFIRMED → 门拒绝。这正是定义书"null/TBD 不能发布为 RELEASED"的落地。
- 跨修订结果复用、热/结构/Fluent、HPC、自动 CAD：R0 明确排除。

## 目录

```
contracts/        OpenAPI 0.1.1 + task-draft/spec JSON Schema + canonical-json-v1 规范
src/dsh_sim/      单包：canonical(冻结) domain db queue api adapters worker verify evidence review mcp capabilities
panels/           执行台 + 审查台（原生 ESM，无构建）
capabilities/     buffer_chamber/0.1.0（DRAFT，阈值全 TBD）
acceptance/       requirements.csv(FR-01..32) test_matrix.csv(TC-001..064) field-acceptance-record.md
compatibility/    P01-P09 探针记录 + 原始证据 JSON
docs/             mcp-integration.md（dsh 接入与验收 checklist）
CONVENTIONS.md    跨 Agent 开发契约（红线/边界/冻结接口）
```

## 已知技术债（下一步）

1. OpenAPI 0.1.1 读取投影为 x-extension 增量，需 Agent A 复审并入正式契约（WP-23）。
2. 面板 DSH 原生 sidebar 槽位插件未做——当前为定义书允许的"受保护审查页"降级实现，需定期评估升级（panels/README.md 有期限说明）。
3. Worker MPI 进程组控制未实测（TBD-02 探针）；cli_adapter 的生产路径需 P04/P06 解锁后补。
4. `ccmpsuite_init` license 缺失→新算例初始化不可用，需管理员确认（TBD-07）。
5. ~~共享 env mcp 版本冲突~~（已解决 2026-09-21）：fastmcp 4.x 强制 mcp>=2，曾把共享 default env 的 mcp 1.28.1 顶到 2.2.0 导致 medini/catia 等桥全崩。修复：MCP server 迁独立 venv `envs/dsh-sim`（pyproject 中 fastmcp 降为 `[mcp]` extra），default env 恢复 mcp==1.28.1。**教训：共享 env 装包前必查依赖对 mcp 的约束**。
