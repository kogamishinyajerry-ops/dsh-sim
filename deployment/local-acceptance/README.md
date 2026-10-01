# deployment/local-acceptance · 本地实机验收运行环境（2026-10-01）

本轮（本地记录）实际使用的可运行交付：单容器 linux/amd64 内运行工程 API + 常驻
worker + OpenFOAM v1912 + SQLite + artifacts；Mac 侧隔离 DSH 通过 127.0.0.1:8600
回环访问。配套的 Assets 侧 overlay 见
JerryDSH-Assets `overlays/`（同一返修轮，两仓配套提交，SHA 见下表）。

## 文件

| 文件 | 用途 |
| --- | --- |
| `launch-worker.sh` | 常驻 worker 启动脚本（容器内实际部署版；关键：先 source 发行版 OpenFOAM bashrc；TEMPLATE_REGISTRY 为 JSON 文件路径） |
| `make-template-registry.py` | 模板注册表 JSON 生成方法（确定性模板 + sha256/boundary_map 输出，供 TaskSpec 登记） |
| `run-local.sh` | 启动/停止/状态/日志（`start`/`stop`/`status`/`logs`） |

## 快速开始（容器已建好后）

```bash
python make-template-registry.py --root /opt/data/openfoam-templates \
  --ref public-openfoam-acc-01 --mean-velocity 0.012 --length 1.2 --height 0.08 \
  --width 0.012 --nu 0.0012 --density 1050 --nx 72 --ny 16 --iterations 600
./run-local.sh start
./run-local.sh status
```

执行台：`http://127.0.0.1:8600/panels/executor/index.html`（dev 身份默认项目
`proj_a`，可用 localStorage `dshsim.projects` 覆盖）。

## 两仓配套提交（同一返修轮，保持 Draft）

| 仓 | 分支 | 提交 | 内容 |
| --- | --- | --- | --- |
| dsh-sim | `codex/p0-local-acceptance-fixes` | `7260236`、`12d1dd8`（基于 `42029ff`） | worker 停止语义、授权读取投影与面板交接恢复、proj_a 默认可覆盖、本目录 |
| JerryDSH-Assets | `codex/p0-local-acceptance-fixes` | `637a894`（基于 `469d7e6`） | 脱敏 headless overlay（标准+600s）与说明 |

## 旧云端记录 vs 新本地记录

- **旧云端记录**（上轮，2026-10-01 早前）：PR head（dsh-sim `5b3ef00`、Assets
  `2ddb273`）时代的验证——378 passed、五场景在 Ubuntu noble amd64 虚拟环境完成、
  证据包 `JerryDSH-P0-Evidence-2026-10-01.zip`（SHA-256 `85e3a78dc0534f5e2de842467b27d98f775835742f67f5540e2e956db02de9bc`）。
  这些记录保留在 `docs/p0-validation-2026-10-01.md/json`，不因本轮而改写。
- **新本地记录**（本轮，本机 Docker Desktop 容器）：修复分支上的 392 passed
  （含 8+3 项新增回归）、五场景重跑、自然语言四阶段全链、盲测/失败/取消验证。
  完整清单见验收目录 `local-acceptance.md/json`（随交接包交付）。
- 求解适配器、验证链路在两轮之间零改动（`adapters/`、`validation/`、`verify/`、
  `evidence/` 在 `5b3ef00..7260236` 无 diff；adapter SHA-256 `e0ccde08…39dc`
  两轮一致）；`worker/loop.py` 仅新增可选 `should_stop` 领取边界参数（11 行，
  不改变作业处理路径），因此五场景数值可直接对照。
