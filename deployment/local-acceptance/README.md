# deployment/local-acceptance · 本地实机验收运行环境（2026-10-01）

本轮（本地记录）实际使用的可运行交付：单容器 linux/amd64 内运行工程 API + 常驻
worker + OpenFOAM v1912 + SQLite + artifacts；Mac 侧隔离 DSH 通过 127.0.0.1:8600
回环访问。配套的 Assets 侧 overlay 见
JerryDSH-Assets `overlays/`（同一返修轮，两仓配套提交，SHA 见下表）。

## 文件

| 文件 | 用途 |
| --- | --- |
| `run-local.sh` | 启动/停止/状态/日志（宿主执行，驱动容器）。启动成功=进程存活+服务健康双重确认，失败非零；停止三态（已请求→正在退出→已退出），确认退出才报 stopped，超时报 NOT stopped 非零 |
| `test-run-local.sh` | run-local 回归（8 项）：Docker 返回码 42 负例、启动失败非零、延迟退出 worker 三态停止、TERM 无视进程 NOT stopped、全新数据目录 registry→start→stop |
| `launch-worker.sh` | 常驻 worker 启动脚本（由 run-local.sh `docker cp` **显式安装**进容器；路径全部从 DSH_SIM_DATA_DIR 派生；先 source 发行版 OpenFOAM bashrc——Ubuntu 包在 /usr/share/openfoam/etc/bashrc；禁用 set -u，该 bashrc 在 set -u 下 source 失败） |
| `make-template-registry.py` | 模板注册表 JSON 生成（ref 白名单校验+路径穿越防护+解析目标必须位于 root 内+原子写 registry；校验先于任何写入） |

## 快速开始（容器已建好后）

```bash
# 宿主执行 run-local.sh；命令在容器内安装/启动
./run-local.sh start          # 安装 launch-worker.sh + 启动 API/worker + 健康确认
./run-local.sh status
./test-run-local.sh           # 8 项回归（含 42 负例、延迟退出、全新数据目录）

# 容器内生成模板注册表（等价宿主 docker exec ...）
docker exec jerrydsh-sim-env bash -c 'cd /opt/dsh-sim && /opt/venv/bin/python \
  deployment/local-acceptance/make-template-registry.py --root /opt/data/openfoam-templates \
  --ref public-openfoam-acc-01 --mean-velocity 0.012 --length 1.2 --height 0.08 \
  --width 0.012 --nu 0.0012 --density 1050 --nx 72 --ny 16 --iterations 600'
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
