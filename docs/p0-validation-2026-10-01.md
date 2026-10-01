# P0 实际验证记录 · 2026-10-01

本记录来自公开数值算例、真实 OpenFOAM 和现有工程核心。机器可读来源为 [同名 JSON](p0-validation-2026-10-01.json)。原始冻结 artifact、日志和报告随可下载证据包提供；运行数据库和任何生产授权不在导出中。

## 结果

| 场景 | 执行 | 数值 | 适用性 | 冻结文件 | 进程组退出 |
| --- | --- | --- | --- | ---: | --- |
| baseline | SUCCEEDED | INSUFFICIENT | UNCONFIRMED | 65 | True |
| transfer | SUCCEEDED | INSUFFICIENT | UNCONFIRMED | 65 | True |
| solver-failure | FAILED | NOT_CHECKED | UNCONFIRMED | 48 | True |
| cancel | CANCELLED | NOT_CHECKED | UNCONFIRMED | 50 | True |
| timeout | FAILED | NOT_CHECKED | UNCONFIRMED | 50 | True |

两组成功求解均到达配置的迭代终点；没有据此声称工程收敛或方法已批准。失败场景保存真实错误，取消和超时都发生在 `simpleFoam` 已输出 `Time =` 后，退出证明的剩余 PID 列表为空。超时事件的业务退出码为 124，受控进程实际响应终止的返回码为 130；二者分别保留。

## 物理观察

| 场景 | 真实静压差 Pa | 解析参考 Pa | 相对差异 | 入口质量流量 kg/s | 出口质量流量 kg/s |
| --- | ---: | ---: | ---: | ---: | ---: |
| baseline | 11.90862000 | 12.00000000 | 0.76150% | -0.01 | 0.0099999999999984 |
| transfer | 31.85716542 | 32.06250000 | 0.64042% | -0.013680000000003 | 0.013680000000002 |

解析参考采用充分发展平行板通道 `12ρνUL/H²`；误差是诊断量，不是本轮创建的接受阈值。实际压力来自官方 VTK 导出的求解场并按密度换算，质量流量来自原始 phi；不是用解析解补填。完整面值和转换来源留在 `conversion-evidence.json`。

## 执行环境与测试

- Python 3.12.14；专用 venv 的依赖版本见 [requirements](../requirements/validation-2026-10-01.txt)。
- OpenCFD v1912，Ubuntu 包 1912.200626-2build3，串行 Linux。官方 Ubuntu InRelease 签名、包索引和 deb SHA-256 已验证；工作区解包，未改宿主系统或用户 Mac。
- 完整命令 `python -m pytest -q -m ''`：**378 passed in 30.91s**，包含 5 项真实求解器测试，0 skip。
- 五条 `python -m dsh_sim.validation.openfoam --local-validation --case ... --output ...` 均按 [操作说明](openfoam-validation.md) 使用新目录完成。
- 五个真实证据导出搬到新目录后全部通过独立哈希验证；在复制件中改动一个 artifact 字节后验证拒绝，原始证据未改。
- 独立审查把 VTK 面顺序及相应 p/U 同时倒序，指标保持一致；单独移动面中心被拒绝，证明对应关系依赖真实几何。

## 已修复的实测问题

1. 真实任务极速耗尽轮询次数、超时未停进程、取消未核实退出：补预算、心跳续租和进程组证明。
2. REAL metrics JSON 首行被错误去除、旧 attempt 可能混入 Claim：按当前 attempt 精确读取，历史失败继续冻结。
3. 缺失必需指标、未知或非有限规则可能产生 PASS：统一 fail-closed，工程阈值仍为 null。
4. 当前容器中 host /proc PID 与 Popen namespace PID 不同：记录两侧 PID、namespace、boot ID、start ticks，信号发送前验证身份。
5. SQLite 路径中的 ? 被 URL 字符串解释为查询：改用 SQLAlchemy URL 对象并用 sentinel 旧库回归验证零写入。
6. v1912 functionObjects 发生 SHA1 stream 错误：保留失败日志，固定配方改用独立官方导出器。

## 仍未验收

- 真实 LLM/自然语言到报告端到端：无模型连接，本轮 NOT_RUN；DSH 原生装载、范围约束和 MCP 握手另在 Assets 仓记录。
- 生产 IdP、正式方法发布、工程 ACCEPT：仍未实现或未获得批准。
- Worker 崩溃后的自动运行接管：未实现；已有持久句柄和 WAL 不代表自动恢复完成。
- STAR 完整适配器、MPI/HPC、其他 OpenFOAM 版本、sim-live-hub：未验证。
- 公开 GitHub 检索未发现与交接对应的 sim-live-hub；缺少其真实仓库和 docs/spec.md，未编造兼容合同。

## 源码绑定

OpenFOAM adapter 源码 SHA-256：`e0ccde081c6945cdf36595ce2a68d8dc99d6f3b4b76f6c65d2c056f1213639dc`。每个 job 的环境探针也保存实际 solver 二进制摘要。TaskSpec 和 Bundle 摘要详见 JSON；这些摘要说明内容一致性，不代替身份签名或工程批准。
