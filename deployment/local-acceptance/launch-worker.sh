#!/bin/bash
# 常驻 worker 启动脚本（本机验收容器内实际部署的版本；路径按需替换）。
# 用法：容器内 nohup /opt/data/launch-worker.sh >> /opt/data/worker.log 2>&1 &
#
# 关键点：
# - 必须先 source 发行版 OpenFOAM 环境（Ubuntu 包在 /usr/share/openfoam/etc/bashrc；
#   OpenCFD tgz 布局在 /usr/lib/openfoam/openfoam1912/etc/bashrc），否则 probe 失败。
# - DSH_SIM_OPENFOAM_TEMPLATE_REGISTRY 是【模板注册表 JSON 文件路径】，
#   不是 capability ID；生成方法见同目录 make-template-registry.py。
set -u
source /usr/share/openfoam/etc/bashrc 2>/dev/null || true
cd /opt/dsh-sim
export DSH_SIM_IDENTITY_MODE=dev
export DSH_SIM_DATABASE_URL=sqlite:////opt/data/dsh_sim.db
export DSH_SIM_ARTIFACT_ROOT=/opt/data/artifacts
export DSH_SIM_WORKER_ADAPTER=openfoam
export DSH_SIM_OPENFOAM_TEMPLATE_REGISTRY=/opt/data/openfoam-templates/registry.json
export DSH_SIM_OPENFOAM_TEMPLATE_ROOT=/opt/data/openfoam-templates
export DSH_SIM_WORKER_WORK_DIR=/opt/data/worker
export DSH_SIM_WORKER_NODE_ID=linux-amd64-container-01
exec /opt/venv/bin/python -m dsh_sim.worker.service
