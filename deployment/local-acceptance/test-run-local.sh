#!/bin/bash
# run-local.sh 的退出码与停止语义回归（在宿主机执行；需要验收容器在运行）。
# 用法: ./test-run-local.sh [容器名]   （默认 jerrydsh-sim-env）
# 覆盖:
#   T1 受检 docker exec 的非零（含 42）必须传播（selfcheck-docker 负例）
#   T2 启动失败必须非零（容器不存在）
#   T3 停止三态: 已请求→正在退出→已退出，延迟退出 worker（3s）后 stopped，rc=0
#   T4 停止超时: 忽略 SIGTERM 的进程 → NOT stopped，rc!=0（不吞错）
#   T5 全新数据目录: start → 进程+服务健康 → stop → 已退出（真实 API+worker）
set -u
CONTAINER="${1:-jerrydsh-sim-env}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PASS=0; FAIL=0

ok()  { echo "PASS: $*"; PASS=$((PASS+1)); }
bad() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }
expect_rc() { # expect_rc <want_nonzero 0|1> <desc> <cmd...>
  local want="$1" desc="$2"; shift 2
  local out rc
  out=$("$@" 2>&1); rc=$?
  if [ "$want" = "0" ] && [ "$rc" -eq 0 ]; then ok "$desc"; 
  elif [ "$want" = "nz" ] && [ "$rc" -ne 0 ]; then ok "$desc (rc=$rc)"
  else bad "$desc (rc=$rc) out: ${out:0:200}"; fi
}

echo "== T1: docker exec 42 负例传播 =="
out=$(DSH_SIM_CONTAINER="$CONTAINER" "$HERE/run-local.sh" selfcheck-docker 2>&1)
rc=$?
if [ "$rc" -eq 0 ] && echo "$out" | grep -q "negative check passed"; then
  ok "T1 selfcheck-docker 42 负例保留且传播"
else
  bad "T1 selfcheck rc=$rc out: ${out:0:300}"
fi

echo "== T2: 启动失败非零（容器不存在） =="
out=$(DSH_SIM_CONTAINER="nonexistent-$RANDOM" "$HERE/run-local.sh" start-api 2>&1); rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -q "ERROR"; then
  ok "T2 start failure propagated non-zero"
else
  bad "T2 rc=$rc out: ${out:0:200}"
fi

TD="/opt/data-rl-test-$(date +%s)"
# 用占位符+文件法生成内层脚本（避免四层引号嵌套——内联版曾因转义产出字面量 $1 导致假 worker 永生）
spawn_fake() { # $1=trap 动作（如 "sleep 3; exit 0" 或 ":"）
  local inner="/tmp/spawn-fake-inner.$$.sh"
  cat > "$inner" <<'INNER'
TD="__TD__"
ACTION="__ACTION__"
bash -c "echo \$\$ > $TD/worker.pid; trap \"$ACTION\" TERM; while :; do sleep 1; done" >/dev/null 2>&1 &
INNER
  sed -e "s|__TD__|$TD|g" -e "s|__ACTION__|$1|g" "$inner" > "$inner.rendered"
  docker cp "$inner.rendered" "$CONTAINER:/tmp/spawn-fake-inner.sh" || { rm -f "$inner" "$inner.rendered"; echo ""; return 1; }
  rm -f "$inner" "$inner.rendered"
  docker exec "$CONTAINER" bash -c "mkdir -p '$TD' && bash /tmp/spawn-fake-inner.sh"
  sleep 0.6
  docker exec "$CONTAINER" bash -c "cat $TD/worker.pid 2>/dev/null"
}

echo "== T3: 延迟退出 worker（3s）三态停止 =="
FID=$(spawn_fake "sleep 3; exit 0")
[ -n "$FID" ] && ok "T3 fake worker spawned (pid $FID)" || bad "T3 spawn failed"
out=$(DSH_SIM_CONTAINER="$CONTAINER" DSH_SIM_DATA_DIR="$TD" DSH_SIM_STOP_TIMEOUT=30 \
      "$HERE/run-local.sh" stop 2>&1); rc=$?
if [ "$rc" -eq 0 ] && echo "$out" | grep -q "已请求" && echo "$out" | grep -q "正在退出" \
   && echo "$out" | grep -q "已退出" && echo "$out" | grep -q "stopped"; then
  ok "T3 three-state stop with delayed exit"
else
  bad "T3 rc=$rc out: ${out:0:400}"
fi

echo "== T4: 忽略 SIGTERM 的进程 → NOT stopped（非零） =="
FID=$(spawn_fake ":")   # trap ':' TERM = 忽略
out=$(DSH_SIM_CONTAINER="$CONTAINER" DSH_SIM_DATA_DIR="$TD" DSH_SIM_STOP_TIMEOUT=4 \
      "$HERE/run-local.sh" stop 2>&1); rc=$?
docker exec "$CONTAINER" bash -c "kill -KILL \$(cat $TD/worker.pid 2>/dev/null) 2>/dev/null; true"
if [ "$rc" -ne 0 ] && echo "$out" | grep -q "NOT stopped"; then
  ok "T4 unkillable-by-TERM worker reports NOT stopped with non-zero"
else
  bad "T4 rc=$rc out: ${out:0:400}"
fi
docker exec "$CONTAINER" bash -c "rm -rf '$TD'"

echo "== T5: 全新数据目录 registry→start→healthy→stop（真实 API+worker） =="
FRESH="/opt/data-fresh-$(date +%s)"
docker exec "$CONTAINER" mkdir -p "$FRESH"
out=$(docker exec "$CONTAINER" bash -c "cd /opt/dsh-sim && /opt/venv/bin/python \
  deployment/local-acceptance/make-template-registry.py --root $FRESH/openfoam-templates \
  --ref public-openfoam-t5 --mean-velocity 0.012 --length 1.2 --height 0.08 \
  --width 0.012 --nu 0.0012 --density 1050 --nx 72 --ny 16 --iterations 50" 2>&1); rc=$?
if [ "$rc" -eq 0 ] && echo "$out" | grep -q '"sha256"'; then
  ok "T5a0 make-template-registry.py 在全新目录生成注册表"
else
  bad "T5a0 rc=$rc out: ${out:0:300}"
fi
out=$(DSH_SIM_CONTAINER="$CONTAINER" DSH_SIM_DATA_DIR="$FRESH" DSH_SIM_START_TIMEOUT=60 \
      "$HERE/run-local.sh" start 2>&1); rc=$?
if [ "$rc" -eq 0 ] && echo "$out" | grep -q "API started" && echo "$out" | grep -q "worker started"; then
  ok "T5a fresh data dir start (process+health verified)"
else
  bad "T5a rc=$rc out: ${out:0:400}"
fi
out=$(DSH_SIM_CONTAINER="$CONTAINER" DSH_SIM_DATA_DIR="$FRESH" "$HERE/run-local.sh" stop 2>&1); rc=$?
if [ "$rc" -eq 0 ] && echo "$out" | grep -q "stopped"; then
  ok "T5b fresh data dir stop confirmed"
else
  bad "T5b rc=$rc out: ${out:0:400}"
fi
docker exec "$CONTAINER" bash -c "rm -rf '$FRESH'" 2>/dev/null

echo "== 结果: PASS=$PASS FAIL=$FAIL =="
[ "$FAIL" -eq 0 ]
