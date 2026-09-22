# macros/ — 固定写入/回读/导出宏登记

本目录登记 STAR-CCM+ 固定 Java 宏 / 原生 Simulation Operations 定义。分三类（对应适配器接口，定义书 §STAR-CCM+适配器定义）：

- **写入宏**：prepare_case 用，向白名单字段写工况参数。
- **回读宏**：read_actual_settings 用，从真实软件读取已生效设置生成 ReadbackSet。
- **导出宏**：collect_outputs 用，导出原始报告、监控、CSV。

## 登记规则

每个宏一个文件 + 一条登记记录（可集中在 `registry.json`，登记时创建）：

| 字段 | 说明 |
|---|---|
| `macro_id` | 稳定标识 |
| `kind` | `write` / `readback` / `export` |
| `sha256` | 宏文件内容哈希 |
| `recorded_from` | 录宏来源：在哪个真实 STAR-CCM+ 构建上、由谁录制/编写 |
| `star_ccm_build` | 录宏环境构建号（当前一律 `UNCONFIRMED`，待 P01/P03 探针） |
| `test_evidence` | 通过测试的探针记录引用（如 P03 参数写入/回读） |

## 铁律

- 具体方法名、命令行参数和报告读取路径必须根据现场安装版录宏、查本地 API 并实测（定义书 §实现优先级）；禁止按猜测编写。
- 首版运行时**禁止生成新 Java 代码**、反射搜索 API 或用 GUI 坐标兜底关键数值（§不能留给模型自行发挥的部分）。
- 历史旧仓库 README 的"已跑通"不能代替本版兼容探针证据。
- 当前目录为空：宏必须在真实软件环境（TBD-02）上录制后登记。
