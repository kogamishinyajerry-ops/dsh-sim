# cases/ — Gold Case 登记

本目录登记基准案例：基准输入、独立参考与证据（定义书 §能力包与最小知识体系 cases/）。

## 首版 3 个 vs 年度 10 个的区别（定义书 §Gold Case与未见任务协议 原文）

| 资产/评测集 | 首版要求 | 与年度目标关系 |
|---|---|---|
| 首版核心 Gold | 至少 3 个真实、完整、经独立审查的代表性案例；覆盖名义点、较不利边界与 A/B 对比。 | 这是首版发布门建议，不替代年度 10 个 Gold Case 目标。 |
| 年度 Gold 台账 | 保留 10 个 Gold Case 资产化计划与状态；逐例记录完整度和适用任务族。 | 未完成案例不能计入"已验证覆盖"。 |

## Gold Case 要交什么（定义书原文）

需求与用途、几何/网格/输入、方法与规则版本、原始结果、独立参考、差异和容差、方法适用证据、失败模式与审查记录。**只有历史模拟结果但无可靠独立依据的案例可称"回归基准"，不得冒称已完成物理验证。**

## 登记规则

每个案例一个子目录或一条 `<case_id>.case.json` 登记记录，字段：`case_id`、`purpose`、`geometry_mesh_input_ref`（哈希+来源）、`method_rules_version`、`raw_results_ref`、`independent_reference_ref`、`delta_and_tolerance`（容差 null/TBD 待 Owner 冻结）、`applicability_evidence`、`failure_modes`、`review_record_ref`、`status`（DRAFT/RELEASED）。

## 铁律

- 不用待测 Agent 输出自己的标准答案（定义书 §验收体系与测试分层·工程基准；R5 职责）。
- 独立参考与验收人员投入属 TBD-10：无独立评测不得宣称迁移能力。
- 当前目录为空：3 个首版 Gold 属 I4 里程碑交付物（WP-17/WP-18），尚未登记。
