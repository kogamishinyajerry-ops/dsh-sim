# P0：公开 OpenFOAM 执行与证据验证

本实现把真实求解接回已有工程核心：`TaskSpec → Preparation → Run/Attempt → Verification → Bundle`。
DSH 只负责分阶段委派；其他 Harness 可以通过同一组 12 个 MCP 工具访问同一个工程服务。
本文件给出可重复的公开实验入口，实际观测见 [2026-10-01 验证记录](p0-validation-2026-10-01.md)。

## 支持范围

| 项 | 本轮范围 |
| --- | --- |
| 求解器 | OpenCFD OpenFOAM v1912，Ubuntu 包 `1912.200626-2build3`，Linux 串行 |
| 物理场景 | 稳态、不可压、常密度、牛顿层流，二维平行板通道 |
| 几何输入 | 已实现的数值配方，确定性生成网格与字典；不接受任意 CAD/Allrun |
| 可修改字段 | `mean_velocity/inlet/m/s`、`kinematic_viscosity/fluid/m2/s`、`density/fluid/kg/m3` |
| 方法包 | `openfoam_channel/0.1.0`，DRAFT，工程阈值未冻结 |
| Python | package 要求 `>=3.12`；本轮隔离测试 `3.12.14`，原 Windows 基线 `3.13` 未重跑 |
| 执行记录 | 真实日志、命令、代码与可执行文件摘要、PID 身份、退出证明、WAL |
| 未验证 | MPI/HPC、其他 OpenFOAM 版本、STAR 完整真实链、生产身份与现场 UI |

所有固定实验参数是求解输入，不是工程接受阈值。数值参考误差只是观察量。

## 独立环境

使用新的 venv。MCP extra 含 MCP 2.x，不应安装到仍运行 MCP 1.x 桥的共享环境：

```bash
python3 -m venv .venv-openfoam
.venv-openfoam/bin/python -m pip install -r requirements/validation-2026-10-01.txt
.venv-openfoam/bin/python -m pip install --no-deps -e .
```

该 requirements 文件记录本轮测试依赖版本，适用于复现 Linux/Python 3.12 实验。
它不含 solver 二进制，不等于离线 wheel 仓或跨平台依赖锁。OpenFOAM 必须从受信发行版安装，
并先加载该发行版提供的环境，使 `blockMesh`、`checkMesh`、`simpleFoam`、`foamDictionary`、
`foamToVTK` 可执行。适配器探针会检查版本、各命令退出码和二进制摘要，失败时不会退回 Mock。

## 一条命令与五个实验

```bash
.venv-openfoam/bin/python -m dsh_sim.validation.openfoam \
  --local-validation --case baseline --output var/channel-baseline
```

输出目录必须不存在；再次运行使用新的目录，历史数据不会被清理。标准输出为最终 JSON，
标准错误持续显示原 Worker WAL 事件。`--quiet` 只关闭控制台事件显示，WAL 和数据库事件仍保留。

| `--case` | 目的 | 预期执行状态 |
| --- | --- | --- |
| `baseline` | 公开基准通道，80×20×1 网格 | `SUCCEEDED` |
| `transfer` | 同一实现，改变长度、高度、宽度、速度、黏度、密度和网格 | `SUCCEEDED` |
| `solver-failure` | 固定的非法离散格式，验证实际 solver 失败与日志留存 | `FAILED` |
| `cancel` | 看到真实 `simpleFoam` 的 `Time =` 日志后请求取消 | `CANCELLED`，必须证实进程组退出 |
| `timeout` | 真实长算例超过一秒执行预算后清理 | `FAILED`，原因是墙钟预算，必须证实退出 |

实验命令退出码 0 表示该场景的**执行行为与预期相符**；失败场景中的 solver 仍保持失败。
`scenario_assertions_passed` 不代表数值 PASS、方法批准或工程 ACCEPT。

该命令是明确的本地测试夹具：只接受上述公开场景，建立全新 SQLite 数据库，
将测试授权行标为 `LOCAL_VALIDATION_FIXTURE_NOT_HUMAN_APPROVAL`。不接受已有 TaskSpec、
API 地址或数据库地址；不生成可信人工确认、不输出可迁移的生产授权，也不进入 MCP 工具清单。
正常工程任务继续通过原受信人工流程确认准备摘要后授权；模型始终使用 AGENT 身份。

## 目录和监控

```text
report.html                 本次实验总览，分别列 execution / numerical / applicability
summary.json                任务、准备、Run、Bundle 引用及实测诊断
task-spec.json              完整、带单位和来源的业务输入
inputs.json                 公开配方与实验身份说明
environment.json            真实 solver 探针
events.jsonl                已提交的工程服务事件
worker/worker.wal.jsonl      可在运行中观察的持久事件
worker/<run>/attempt-1/      实际独立运行现场及进程、命令、日志
evidence/export.json         冻结 manifest、摘要及可搬迁文件映射
evidence/artifacts/          原始冻结文件，包括原报告字节
evidence/report.offline.html 单独生成的离线链接视图
validation.db               本地夹具数据库；不作为生产数据或授权导出
```

Artifact 本体已带长度和 SHA-256。离线 HTML 只是便利视图，完整性验证针对冻结 manifest
及其 artifact；它不会将派生展示视图变成新工程结论。

把 `evidence/` 复制到任意新目录后，可在没有 DSH 会话或数据库的环境中运行：

```bash
.venv-openfoam/bin/python -c \
  'from dsh_sim.evidence.export import verify_export; print(verify_export("var/copied-evidence"))'
```

相同 `bundle_digest` 与 artifact 摘要必须保持一致；修改任一冻结文件会拒绝。
摘要用于检查内容一致性，不是身份签名或工程批准。

## 真实数值从哪里来

每次 attempt 使用冻结归档解包新算例，固定 argv 执行
`blockMesh → checkMesh → simpleFoam → foamToVTK`。归档只接受已经实现的确定性数值配方；
目录穿越、symlink、重复条目、额外代码、函数注入和不受支持的字段都会拒绝。

`simpleFoam` 的 `p` 是运动学压力；转成 Pa 必须乘常密度。参见
[OpenFOAM 官方说明](https://doc.openfoam.com/2312/tools/processing/solvers/algorithm-kinematic-pressure/)。
本配方保留 solver 的压力参考，不从表压推断未知绝压。

| 量 | 实际来源与转换 |
| --- | --- |
| 质量流量 | 最终时刻原始 `phi`，按边界 `rho × sum(phi)`，向计算域外为正 |
| 静压 | 官方 VTK 导出的实际边界面 `p`，按真实面面积平均后乘 `rho` |
| 总压 | 对每面 `rho × (p + |U|²/2)` 按实际 `abs(phi)` 加权；缺项或回流不适用时留空 |
| 残差 | 真实 `simpleFoam` stdout，每个 iteration 一行；终止原因与收敛判据分别记录 |
| 面对应 | 原始 polyMesh 面中心匹配 VTK polygons，不能假设两个数组顺序相同 |

`conversion-evidence.json` 保留每面值、转换公式、密度、对应关系及源文件摘要。
独立 Verifier 从规范 CSV 复算，必需指标缺失、NaN/Inf、非法规则或未知比较器都不能得到 PASS。

当前 Ubuntu v1912 的 functionObjects 在实测中触发 SHA1 stream 错误，因此受控配方使用
`functions {}` 并单独运行官方 `foamToVTK`。原失败日志保留；没有修改 solver 二进制。
这个导出器的 p/U 约六位有效数字，原始 phi 保留 solver 写出精度；转换证据会注明精度限制。

## 进程与恢复边界

Worker 使用单调时钟预算、真实等待和心跳续租；等待前释放 SQLite 写事务，使取消请求可及时入库。
取消、超时、普通异常和 Ctrl-C 都先尝试停止受控进程组、收集实际现场，再报告状态。
PID 身份包括 boot ID、host/namespace PID、创建 ticks 和进程组；只有身份与退出证明一致才确认停止。

无法证明退出时为 `LOST`，继续占用并发位，不自动补发作业。新 adapter 实例可从持久 job 记录重新
`poll()`，WAL 支持安全重传；**Worker 重启后自动接管仍运行作业尚未实现**。机器失联或强制杀死 Worker
也不能声称已自动清理，现场核实仍是恢复前提。

## 原生 DSH 接入

Assets 仓的 `simulation` 声明式 preset 使用 `sim_orchestrate` 委派
planner / inputwriter / runner / reviewer。子 Agent 只看到该阶段所需的既有 MCP 工具。
长时间求解由本服务继续执行，委派完成不改变工程状态。

本轮已经验证无密钥装载、工具范围和真实 MCP 握手；没有进行 LLM 请求。
当前 MCP 仍只有显式 dev 身份，生产 IdP 接入和在线自然语言端到端需要单独验证。
`sim-live-hub` 的仓库/spec 尚未获得，未创建同名的假合同或假定本仓等同该项目。
