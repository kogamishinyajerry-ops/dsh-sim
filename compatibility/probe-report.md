# dsh-sim · 首个实机探针记录（P01–P09）

- 探针执行人：Agent D（R3 工业接口）｜日期：2026-09-20｜一次性任务
- 依据：定义书 §首个实机探针与停止条件、§STAR-CCM+适配器定义；CONVENTIONS §0 诚实红线
- 执行环境：Windows 11 Home China (10.0.26200.9457)
- STAR-CCM+：**Simcenter STAR-CCM+ 2402 Build 19.02.009 (win64/clang15.0vc14.2-r8 Double Precision)**
  - 主用可执行：`<STARCCM_2402_BAT>`
  - 另存安装：`C:\Program Files\Siemens\17.06.007-R8\...`（未测；版本基线 FR-27 须锁定唯一版本）
- 授权：<LICENSE_FILE>（路径已脱敏），ccmpsuite 可 checkout（P02/P05 spawn 旁证）；**`ccmpsuite_init` 缺失**（license-probe 实跑）；license 合规状态待组织内部确认（TBD-07）
- CLI 桥：`<STARCCM_CLI_DIR>\starccm_cli.py` v49.0.0，83 命令，统一 v3 JSON payload（capabilities 实跑 exit=0）

## 结论一览

| 探针 | 结论 | 关键证据（原样摘录） | 证据文件 |
|---|---|---|---|
| P01 版本/授权 | **PASS** | `Simcenter STAR-CCM+ 2402 Build 19.02.009`；license-probe 实跑：`available=[read_only, basic_geom, ccmpsuite_solve], missing=[ccmpsuite_init]` | P01-version-license.json |
| P02 模板打开/保存 | **PASS** | `[SaveCopyProbe] opened/saved/DONE`；原件 sha256 前后一致 `f5a5c502…` | P02-template-open-save.json |
| P03 参数写入/回读 | **PASS** | `WRITE boundary=z_min … BEFORE=30.0 TARGET=31.0 READBACK=31.0`；重开 `persisted_velocity=31.0` | P03-param-write-readback.json |
| P04 角色绑定 | **NOT_RUN** | 缺批准 boundary-map（TBD-05）；已采集真实边界名/类型签名；面积/质心 API 一次尝试失败未确认 | P04-role-binding.json |
| P05 真算与导出 | **PASS**（限定） | `iter_before=5005 → iter_after=5030`；7 项残差真实导出 CSV；限定：已初始化案例续算，非全新 init | P05-real-solve-export.json |
| P06 重复性 | **NOT_RUN** | 重复性容差为 Owner 冻结项（TBD-06），无容差不判定；技术前置（P05 可重复执行）已具备 | P06-repeatability.json |
| P07 取消清理 | **PASS**（限定） | taskkill /F /T 树 5404→1944→8536→18372 全终止、无残留；后续 batch 重新 checkout 成功；限定：串行 batch，未测 MPI 与优雅停止 | P07-cancel-cleanup.json |
| P08 断线重入 | **PASS（工程服务层限定）** | 2026-09-21 复测：SIGKILL 后 PREPARING+PREPARE 作业存活、重启可 claim；期间发现并修复持久化竞态（202 响应先于提交） | P08-disconnect-rejoin.json / reprobe-2026-09-21.json |
| P09 权限隔离 | **PASS（开发身份头模式限定）** | 2026-09-21 复测：AGENT 授权/跨项目读/职责分离三门全 403；生产 IdP 为 TBD-08 | P09-permission-isolation.json / reprobe-2026-09-21.json |

原始日志/输出在 `compatibility/raw/`。

## 对本机 CLI 桥的重要实测发现（交接实现 Agent）

1. **spawn 语法**：本机 19.02.009 只接受 `[starccm+.bat, <sim>, "-batch", <macro>]`（sim 在前）；旧顺序 `[bat, "-batch", macro, sim]` 报 usage/rc=2。且 **`-batch` 后拒绝额外位置参数**——宏参数必须烘焙进 `.java` 源码（桥自身注释 starccm_cli.py:88-91, 9865-9867，本次实测复现）。
2. **桥 checkpoint 命令缺陷**：`checkpoint` 仍用旧参数顺序，本机实跑 `SPAWN_RC rc=2`（0.08s 空输出）。StarCliAdapter 不得直接复用该命令的 spawn 构造；应统一走 v34 语法 + 参数烘焙。
3. **写入 API**：`bnd.getValues().get(VelocityMagnitudeProfile.class).getMethod(ConstantScalarProfileMethod.class).getQuantity().setValue(v)` 实测有效；`star.flow.VelocityInletBoundary` 在本构建不可 import（编译错误），边界探测走 `getValues().get(...)` 判空，不靠类名猜测（符合定义书"不得将猜测类名当作能力"）。
4. **license 能力边界**：`ccmpsuite_init` 缺失 → 全新算例初始化不可用；已初始化案例续算（P05/P07）与打开/保存/写参（P02/P03）可用。R0 若需从模板新初始化，需站点补齐 init license 或由批准模板预初始化（决策交 Owner/管理员，TBD-07）。
5. **环境变量**：spawn 前须设 `JAVA_TOOL_OPTIONS=-Dfile.encoding=UTF-8`（桥 v34 注释；中文 Windows javac GBK 会炸中文注释宏）。

## NOT_RUN / BLOCKED 解锁条件汇总

- P04：R4/Owner 交付批准 boundary-map.json + WP-06 录宏确认面积/质心签名 API。
- P06：Owner 冻结重复性容差（TBD-06）+ 批准模板/指标定义。
- P07 完整版：MPI（-np>1）进程组控制探针 + 优雅停止（stop file）两级取消补测（Worker 实现期）。
- P08 → 已复测 PASS（工程服务层）；完整版还需常驻 Worker + 真实求解断线演练。
- P09 → 已复测 PASS（开发身份模式）；生产 IdP 集成仍为 TBD-08。
