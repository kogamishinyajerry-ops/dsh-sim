# 首版现场验收脚本 · 执行记录

> 依据定义书 §首版现场验收脚本 12 步。执行人：多 Agent + 编排层。执行日期：2026-09-21。
> 执行模式说明：MOCK = MockStarAdapter 协议链路真实跑通，数据非真实求解；REAL = 真实 STAR-CCM+；NOT_RUN = 未执行。
> 对应自动化证据：`tests/test_mock_chain.py`（端到端六 Run）、`tests/test_review_flow.py`（审查闭环）、`tests/test_verify.py`（数值校核）。全部 `pytest -m mock` 实跑通过（147/147）。

| 步 | 验收动作 | 执行模式 | 结果 | 证据 |
|---|---|---|---|---|
| 01 | 非开发工程师创建 A/B×3 工况任务 | MOCK | PASS：任务卡/来源/方法清晰；缺字段进 blockers（field+responsible+question） | test_mock_chain.py::test_full_chain；冒烟 createTask 实响应含 blockers |
| 02 | 故意遗漏表压参考，随后补齐 | MOCK | PASS：prepare 前校验拒绝（ENGINEERING_INPUT，422）；补齐后新修订合法 | test_verify.py 压力语义用例；domain/schemas.py pressure 校验 |
| 03 | 准备六个副本，抽查一项真实设置 | MOCK | PASS：批准模板哈希不变；回读一致；差异可定位（mismatch 注入可检出） | test_mock_chain.py prepare 段；FR-06/07 映射 |
| 04 | 人工确认并运行，立即重复提交 | MOCK | PASS：同一 Run 集合，无重复真实启动（幂等 409/返回原对象） | test_queue.py 幂等 + test_api_contract.py 幂等中间件 |
| 05 | 关闭 DSH 后再打开 | MOCK | PASS（服务层）：任务/事件/状态从 DB 恢复，非会话文本；Worker WAL 断线重传已测 | test_mock_chain.py WAL 用例；REAL 断线重入 NOT_RUN |
| 06 | 使一个任务缺 CSV 或检查失败 | MOCK | PASS：该 Run 标 INSUFFICIENT；bundle incomplete；整单 ACCEPT 必败 | test_mock_chain.py 负例三 |
| 07 | 按批准策略补算 | MOCK | PASS：新 attempt（UNIQUE(run_id,attempt_no)），旧证据保留；超预算阻塞 | test_queue.py attempt 用例 |
| 08 | 生成证据包并提交指定审查人 | MOCK | PASS：bundle 冻结（长度+sha256 清单）；review PENDING 绑定修订+摘要 | test_evidence.py |
| 09 | 审查人质疑压损定义并提出问题 | MOCK | PASS：issue 五段式（责任人/关闭判据/关联证据）；Agent 只能 DRAFT | test_review_flow.py |
| 10 | 补证、回复，审查人关闭问题 | MOCK | PASS：回复+新证据绑定；Agent 身份关闭被 403 拒绝 | test_review_flow.py 负例 |
| 11 | 审查人接受本轮筛选 | MOCK | PASS（门全绿路径）：decideReview 绑定摘要/修订/用途一次性确认；六类阻塞逐条拒绝路径均实测 | test_review_flow.py |
| 12 | 更改一项输入形成新修订 | MOCK | PASS：旧包 STALE、旧授权失效；历史决定保留可读；新任务不继承接受 | test_review_flow.py 修订用例 |

## REAL 部分（真实 STAR-CCM+）

- 步骤 03 的"真实设置抽查"在真实软件层已由探针 P03 独立通过（compatibility/P03）。
- 步骤 05 真实断线重入、步骤 06-11 在真实求解器上的完整复演：**NOT_RUN**（依赖批准模板 TBD-05、license `ccmpsuite_init`、Owner 冻结阈值 TBD-06）。
- UAT（两名非开发工程师+独立审查人）：**NOT_RUN**（TBD-08/TBD-10）。

## 声明

本记录不含"预计通过"。所有 PASS 均有自动化测试或探针 JSON 证据；所有 NOT_RUN 均有明确解锁条件。故障注入在隔离 demo 项目执行，未触碰正式数据。
