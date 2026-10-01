#!/bin/bash
# 本地验收运行环境：启动/停止（容器内执行）。
# 布局：单容器（linux/amd64）内运行工程 API + 常驻 worker + OpenFOAM + SQLite + artifacts，
#       与宿主通过 127.0.0.1:8600 回环转发交互（docker -p 127.0.0.1:8600:8600）。
# 容器创建：docker run -d --name jerrydsh-sim-env --platform linux/amd64 \
#   -p 127.0.0.1:8600:8600 -v <dsh-sim 仓库>:/opt/dsh-sim -v jerrydsh-sim-data:/opt/data \
#   ubuntu:noble sleep infinity
# 依赖安装见仓库根 docs/openfoam-validation.md（openfoam=1912.200626-2build3 + /opt/venv）。
set -u
ACTION="${1:-help}"

start_api() {
  docker exec -d jerrydsh-sim-env bash -c '
    cd /opt/dsh-sim
    export DSH_SIM_IDENTITY_MODE=dev DSH_SIM_DATABASE_URL=sqlite:////opt/data/dsh_sim.db DSH_SIM_ARTIFACT_ROOT=/opt/data/artifacts
    nohup /opt/venv/bin/python -m uvicorn dsh_sim.api.main:app --host 0.0.0.0 --port 8600 >> /opt/data/api.log 2>&1'
  echo "API starting on 127.0.0.1:8600 (dev identity)"
}

start_worker() {
  docker exec -d jerrydsh-sim-env bash -c 'nohup /opt/data/launch-worker.sh >> /opt/data/worker.log 2>&1'
  echo "persistent worker starting (openfoam adapter; see /opt/data/worker.log)"
}

stop_all() {
  # 只停本验收部署的进程（锚定匹配，避免误伤容器里其他进程）
  docker exec jerrydsh-sim-env bash -c '
    pkill -f "^/opt/venv/bin/python -m uvicorn" 2>/dev/null
    pkill -f "^/opt/venv/bin/python -m dsh_sim[.]worker" 2>/dev/null
    pkill -f "^/bin/bash /opt/data/launch-worker.sh" 2>/dev/null
    true'
  echo "API/worker stopped"
}

case "$ACTION" in
  start)    start_api; start_worker ;;
  start-api) start_api ;;
  start-worker) start_worker ;;
  stop)     stop_all ;;
  status)
    docker exec jerrydsh-sim-env bash -c '
      pgrep -af "^/opt/venv/bin/python -m uvicorn" || echo "API: not running"
      pgrep -af "dsh_sim.worker.service" || echo "worker: not running"'
    curl -s -o /dev/null -w "API health: %{http_code}\n" \
      -H "X-Dev-Subject: probe" -H "X-Dev-Roles: EXECUTOR" -H "X-Dev-Projects: proj_a" \
      http://127.0.0.1:8600/api/v1/tasks || true
    ;;
  logs)     docker exec jerrydsh-sim-env tail -20 /opt/data/api.log /opt/data/worker.log ;;
  *)        sed -n 2,12p "$0"; echo "usage: $0 {start|start-api|start-worker|stop|status|logs}" ;;
esac
