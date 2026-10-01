#!/bin/bash
# 常驻 worker 启动脚本（交付件；由 run-local.sh 通过 docker cp 安装到容器内执行）。
#
# 关键点：
# - 必须先 source 发行版 OpenFOAM 环境（Ubuntu 包 /usr/share/openfoam/etc/bashrc；
#   OpenCFD tgz 布局 /usr/lib/openfoam/openfoam1912/etc/bashrc），否则 probe 失败。
# - DSH_SIM_OPENFOAM_TEMPLATE_REGISTRY 是【模板注册表 JSON 文件路径】
#   （{"引用名": "/abs/path/channel.tar"}），不是 capability ID；
#   生成方法见同目录 make-template-registry.py。
# - 全部数据路径从 DSH_SIM_DATA_DIR 派生（默认 /opt/data），保证"全新数据目录"
#   验证时不写旧目录；模板/注册表默认也在 $DSH_SIM_DATA_DIR/openfoam-templates。
# - 本脚本 exec 目标进程：调用方（run-local.sh start-worker）记录的 $! 即最终
#   python 进程 PID，worker.pid 因此准确。
# 注意：不要在此脚本使用 set -u——Ubuntu 发行版 OpenFOAM bashrc 在 set -u 下
# source 会失败（实测 rc=127），worker 会静默死亡且日志为空。
DATA_DIR="${DSH_SIM_DATA_DIR:-/opt/data}"
REPO_DIR="${DSH_SIM_REPO_DIR:-/opt/dsh-sim}"
VENV_PY="${DSH_SIM_VENV_PY:-/opt/venv/bin/python}"

source /usr/share/openfoam/etc/bashrc 2>/dev/null || true
cd "$REPO_DIR"
export DSH_SIM_IDENTITY_MODE="${DSH_SIM_IDENTITY_MODE:-dev}"
export DSH_SIM_DATABASE_URL="${DSH_SIM_DATABASE_URL:-sqlite:///$DATA_DIR/dsh_sim.db}"
export DSH_SIM_ARTIFACT_ROOT="${DSH_SIM_ARTIFACT_ROOT:-$DATA_DIR/artifacts}"
export DSH_SIM_WORKER_WORK_DIR="${DSH_SIM_WORKER_WORK_DIR:-$DATA_DIR/worker}"
export DSH_SIM_WORKER_ADAPTER="${DSH_SIM_WORKER_ADAPTER:-openfoam}"
export DSH_SIM_OPENFOAM_TEMPLATE_REGISTRY="${DSH_SIM_OPENFOAM_TEMPLATE_REGISTRY:-$DATA_DIR/openfoam-templates/registry.json}"
export DSH_SIM_OPENFOAM_TEMPLATE_ROOT="${DSH_SIM_OPENFOAM_TEMPLATE_ROOT:-$DATA_DIR/openfoam-templates}"
export DSH_SIM_WORKER_NODE_ID="${DSH_SIM_WORKER_NODE_ID:-linux-amd64-container-01}"
exec "$VENV_PY" -m dsh_sim.worker.service
