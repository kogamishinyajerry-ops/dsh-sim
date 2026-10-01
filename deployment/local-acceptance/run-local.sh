#!/bin/bash
# 本地验收运行环境控制脚本（在【宿主机】执行，通过 docker exec 驱动【容器内】进程）。
#
# 布局：单容器（linux/amd64）内运行工程 API + 常驻 worker + OpenFOAM + SQLite + artifacts；
#       宿主通过 127.0.0.1:8600 回环访问（docker -p 127.0.0.1:8600:8600）。
#
# 宿主执行（本脚本）:
#   ./run-local.sh start | start-api | start-worker | stop | status | logs | selfcheck-docker
# 容器内手工等价命令（调试用）:
#   docker exec -it "$CONTAINER" bash
#   docker exec "$CONTAINER" tail -n 40 /opt/data/api.log
#
# 环境变量（均可覆盖）: DSH_SIM_CONTAINER / DSH_SIM_DATA_DIR / DSH_SIM_REPO_DIR /
#   DSH_SIM_VENV_PY / DSH_SIM_PORT / DSH_SIM_START_TIMEOUT / DSH_SIM_STOP_TIMEOUT /
#   DSH_SIM_IDENTITY_MODE / DSH_SIM_AGENT_PROJECTS
set -u
CONTAINER="${DSH_SIM_CONTAINER:-jerrydsh-sim-env}"
DATA_DIR="${DSH_SIM_DATA_DIR:-/opt/data}"
REPO_DIR="${DSH_SIM_REPO_DIR:-/opt/dsh-sim}"
VENV_PY="${DSH_SIM_VENV_PY:-/opt/venv/bin/python}"
PORT="${DSH_SIM_PORT:-8600}"
START_TIMEOUT="${DSH_SIM_START_TIMEOUT:-30}"
STOP_TIMEOUT="${DSH_SIM_STOP_TIMEOUT:-60}"
IDENTITY_MODE="${DSH_SIM_IDENTITY_MODE:-dev}"
AGENT_PROJECTS="${DSH_SIM_AGENT_PROJECTS:-proj_a}"
HERE="$(cd "$(dirname "$0")" && pwd)"

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[run-local] $*"; }

# 宿主→容器的受检执行：非零退出码必须传播（含返回码 42 这类命令退出码）。
dc() { docker exec "$CONTAINER" "$@"; }
checked() { if ! "$@"; then die "command failed (rc=$?): $*"; fi; }

require_container() {
  docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" \
    || die "container '$CONTAINER' is not running"
}

# 显式安装步骤：launch-worker.sh 随本目录交付，docker cp 安装进容器（幂等覆盖）。
install_worker_script() {
  [ -f "$HERE/launch-worker.sh" ] || die "delivered launch-worker.sh missing next to run-local.sh"
  docker cp "$HERE/launch-worker.sh" "$CONTAINER:$DATA_DIR/launch-worker.sh" \
    || die "docker cp launch-worker.sh failed"
  checked docker exec "$CONTAINER" chmod +x "$DATA_DIR/launch-worker.sh"
  log "launch-worker.sh installed at $DATA_DIR/launch-worker.sh"
}

# 容器 PID1 是 `sleep infinity`，不收尸：已退出进程永久留为 zombie，而 kill -0
# 对 zombie 返回成功。"真存活" = kill -0 成功 且 /proc/<pid>/State != Z。
list_live_pids() {
  dc bash -c "for p in $1; do
    if kill -0 \$p 2>/dev/null; then
      st=\$(awk '/^State:/{print \$2; exit}' /proc/\$p/status 2>/dev/null)
      [ "\$st" != Z ] && echo \$p
    fi
  done"
}

# 启动成功 = 目标进程存活 + 服务健康检查通过（两者都确认才算 started）。
wait_api_healthy() {
  local deadline=$(( SECONDS + START_TIMEOUT ))
  while (( SECONDS < deadline )); do
    if [ -z "$(list_live_pids "$(dc cat "$DATA_DIR/api.pid" 2>/dev/null)")" ]; then
      die "API process exited during startup; log: docker exec $CONTAINER tail -n 40 $DATA_DIR/api.log"
    fi
    if curl -fsS -m 2 -H "X-Dev-Subject: probe" -H "X-Dev-Roles: EXECUTOR" \
        -H "X-Dev-Projects: $AGENT_PROJECTS" \
        "http://127.0.0.1:$PORT/api/v1/tasks" >/dev/null 2>&1; then
      # 健康通过后复核本次启动的进程仍存活（防止端口被残留旧进程顶替的假阳性）。
      if [ -z "$(list_live_pids "$(dc cat "$DATA_DIR/api.pid" 2>/dev/null)")" ]; then
        die "port $PORT healthy but the API process we started has exited (port likely held by a stale process); clean it and retry"
      fi
      log "API started: process alive + service healthy (port $PORT)"
      return 0
    fi
    sleep 1
  done
  die "API health check timeout after ${START_TIMEOUT}s (process alive but service not ready)"
}

wait_worker_alive() {
  local oldsize="${1:-0}"
  local deadline=$(( SECONDS + START_TIMEOUT ))
  while (( SECONDS < deadline )); do
    if [ -z "$(list_live_pids "$(dc cat "$DATA_DIR/worker.pid" 2>/dev/null)")" ]; then
      die "worker process exited during startup; log: docker exec $CONTAINER tail -n 40 $DATA_DIR/worker.log"
    fi
    # 只检查本次启动新增的日志字节，避免旧日志造成假阳性。
    if dc bash -c "tail -c +$((oldsize + 1)) '$DATA_DIR/worker.log' 2>/dev/null | grep -q 'worker started'"; then
      log "worker started: process alive + 'worker started' logged"
      return 0
    fi
    sleep 1
  done
  die "worker start timeout after ${START_TIMEOUT}s"
}

start_api() {
  require_container
  if [ -n "$(list_live_pids "$(dc cat "$DATA_DIR/api.pid" 2>/dev/null)")" ]; then
    log "API already running (pid $(dc cat "$DATA_DIR/api.pid" 2>/dev/null))"
    return 0
  fi
  log "starting API in $CONTAINER (data dir $DATA_DIR)..."
  checked docker exec "$CONTAINER" bash -c "
    mkdir -p '$DATA_DIR'
    cd '$REPO_DIR'
    export DSH_SIM_IDENTITY_MODE='$IDENTITY_MODE' DSH_SIM_AGENT_PROJECTS='$AGENT_PROJECTS'
    export DSH_SIM_DATABASE_URL='sqlite:///$DATA_DIR/dsh_sim.db' DSH_SIM_ARTIFACT_ROOT='$DATA_DIR/artifacts'
    nohup $VENV_PY -m uvicorn dsh_sim.api.main:app --host 0.0.0.0 --port $PORT \
      >> '$DATA_DIR/api.log' 2>&1 &
    echo \$! > '$DATA_DIR/api.pid'"
  wait_api_healthy
}

start_worker() {
  require_container
  install_worker_script   # 显式安装/更新交付的启动脚本（不依赖容器内历史残留文件）
  if [ -n "$(list_live_pids "$(dc cat "$DATA_DIR/worker.pid" 2>/dev/null)")" ]; then
    log "worker already running (pid $(dc cat "$DATA_DIR/worker.pid" 2>/dev/null))"
    return 0
  fi
  log "starting persistent worker (openfoam adapter; paths derived from DSH_SIM_DATA_DIR)..."
  local oldsize
  oldsize="$(dc bash -c "stat -c %s '$DATA_DIR/worker.log' 2>/dev/null || echo 0")"
  checked docker exec "$CONTAINER" bash -c "
    export DSH_SIM_DATA_DIR='$DATA_DIR' DSH_SIM_REPO_DIR='$REPO_DIR' DSH_SIM_VENV_PY='$VENV_PY'
    nohup '$DATA_DIR/launch-worker.sh' >> '$DATA_DIR/worker.log' 2>&1 &
    echo \$! > '$DATA_DIR/worker.pid'"
  wait_worker_alive "$oldsize"
}

# 目标进程 = pid 文件存活项 ∪ 锚定模式残留项（处理历史启动、无 pid 文件的进程）。
list_target_pids() {
  dc bash -c "cat '$DATA_DIR/api.pid' '$DATA_DIR/worker.pid' 2>/dev/null; pgrep -f '^/opt/venv/bin/python -m (uvicorn dsh_sim[.]api[.]main:app|dsh_sim[.]worker[.]service)' 2>/dev/null" | sort -u
}

# 停止三态：已请求（SIGTERM 已发）→ 正在退出（进程仍在，等待中）→ 已退出（确认后才
# 声称 stopped）。超时 = NOT stopped（非零退出），绝不吞错、绝不谎报已停止。
stop_all() {
  require_container
  local pids alive remain deadline
  # 归一为单行空格分隔（list_target_pids 可能多行；容器端 for 循环需要单行）
  pids="$(list_target_pids | tr '\n' ' ')"
  if [ -z "$pids" ]; then
    log "already stopped (no live target processes)"
    return 0
  fi
  log "已请求: sending SIGTERM to: $(echo "$pids" | tr '\n' ' ')"
  dc bash -c "for p in $pids; do kill -TERM \$p 2>/dev/null; done; true"
  deadline=$(( SECONDS + STOP_TIMEOUT ))
  while :; do
    alive="$(list_live_pids "$pids")"
    if [ -z "$alive" ]; then
      log "已退出: all target processes confirmed exited"
      dc bash -c "rm -f '$DATA_DIR/api.pid' '$DATA_DIR/worker.pid'"
      log "stopped"
      return 0
    fi
    remain=$(( deadline - SECONDS ))
    if (( remain <= 0 )); then
      die "NOT stopped: processes still alive after ${STOP_TIMEOUT}s: $(echo "$alive" | tr '\n' ' ')"
    fi
    log "正在退出: alive=($(echo "$alive" | tr '\n' ' ')) waiting, ${remain}s left"
    sleep 2
  done
}

status() {
  require_container
  for p in $(dc bash -c "cat '$DATA_DIR/api.pid' '$DATA_DIR/worker.pid' 2>/dev/null"); do
    if [ -n "$(list_live_pids "$p")" ]; then echo "pid $p: running"; else echo "pid $p: exited"; fi
  done
  dc bash -c "pgrep -af '^/opt/venv/bin/python -m (uvicorn dsh_sim[.]api[.]main:app|dsh_sim[.]worker[.]service)' 2>/dev/null" || true
  if curl -fsS -m 2 -H "X-Dev-Subject: probe" -H "X-Dev-Roles: EXECUTOR" \
      -H "X-Dev-Projects: $AGENT_PROJECTS" \
      "http://127.0.0.1:$PORT/api/v1/tasks" >/dev/null 2>&1; then
    log "service health: healthy (port $PORT)"
  else
    log "service health: NOT healthy (port $PORT)"
  fi
}

selfcheck_docker() {
  # 负例（保留）：受检 docker exec 的非零退出码（含 42）必须传播，不得吞掉。
  checked docker exec "$CONTAINER" bash -c 'true'
  log "positive check passed (rc=0)"
  local rc=0
  docker exec "$CONTAINER" bash -c 'exit 42' || rc=$?
  if [ "$rc" -eq 42 ]; then
    log "negative check passed: docker exec rc=42 observed and treated as failure"
    return 0
  fi
  die "selfcheck broken: expected rc=42 from 'exit 42', got rc=$rc"
}

case "${1:-help}" in
  start)        start_api; start_worker ;;
  start-api)    start_api ;;
  start-worker) start_worker ;;
  stop)         stop_all ;;
  restart)      "$0" stop || die "restart aborted: stop did not reach 'stopped'"; start_api; start_worker ;;
  status)       status ;;
  logs)         require_container; dc bash -c "tail -n 20 '$DATA_DIR/api.log' '$DATA_DIR/worker.log'" ;;
  selfcheck-docker) selfcheck_docker ;;
  *)
    sed -n '2,16p' "$0"
    echo "usage: $0 {start|start-api|start-worker|stop|restart|status|logs|selfcheck-docker}"
    ;;
esac
