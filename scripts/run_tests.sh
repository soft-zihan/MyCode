#!/usr/bin/env bash
# 双后端回归门禁（升级方案 M1-3：CI 双矩阵常态化）。
#
# pytest 腿：进程内跑，MYCODE_SESSION_BACKEND 环境变量切换即可。
# smoke 腿：eval.smoke.runner 是 HTTP/WS 客户端，后端必须带对应 backend 重启，
#           本脚本自动 ./start.sh restart 并等待健康检查。
#
# 用法:
#   ./scripts/run_tests.sh              # pytest × (jsonl + sqlite)
#   ./scripts/run_tests.sh --smoke      # pytest × 2 + smoke chain（jsonl 腿，日常）
#   ./scripts/run_tests.sh --smoke-only # 只跑 smoke（jsonl 腿，评测任务迭代）
#   ./scripts/run_tests.sh --smoke-dual # smoke 双腿（存储层改动/里程碑收口专用）
#   其余参数透传给 pytest（如 -k fork）
#
# 门禁分级（2026-09-23 政策，gate4-8 复盘依据）：pytest 双跑成本≈40s 保留常态；
# smoke sqlite 腿 13min 且历史上从未抓到 sqlite 特有产品 bug（失败均为双腿共享的
# 评测设计缺陷）——日常只跑 jsonl 腿，存储层改动（session*/backend/agent/agent_loop/
# wiki_capture/前端 sessions router）与里程碑收口必须 --smoke-dual 全绿。
# smoke 腿结束后服务恢复默认后端。
set -euo pipefail
cd "$(dirname "$0")/.."

# 防双启动：并发 gate 会互相 start.sh restart 抽走对方后端，产生成批假失败
LOCKDIR="logs/.run_tests.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  echo "❌ 另一个 run_tests.sh 正在运行（pid=$(cat "$LOCKDIR/pid" 2>/dev/null || echo '?')）；确认无进程后 rm -rf $LOCKDIR 可解锁" >&2
  exit 1
fi
echo $$ > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR"' EXIT

PY=.venv/bin/python
RUN_SMOKE=0
SMOKE_ONLY=0
SMOKE_DUAL=0
PYTEST_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --smoke) RUN_SMOKE=1 ;;
    --smoke-only) RUN_SMOKE=1; SMOKE_ONLY=1 ;;
    --smoke-dual) RUN_SMOKE=1; SMOKE_DUAL=1 ;;
    *) PYTEST_ARGS+=("$arg") ;;
  esac
done

pytest_leg() {
  local backend="$1"
  echo ""
  echo "════════ pytest [${backend}] ════════"
  MYCODE_SESSION_BACKEND="$backend" "$PY" -m pytest tests/ -q --ignore=tests/e2e ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}
}

wait_health() {
  for i in $(seq 1 60); do
    if curl -sf http://127.0.0.1:5555/api/health >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "❌ 后端 60s 内未就绪，见 logs/.backend.log" >&2
  exit 1
}

smoke_leg() {
  local backend="$1"
  echo ""
  echo "════════ smoke chain [${backend}]（重启服务端）════════"
  MYCODE_SESSION_BACKEND="$backend" ./start.sh restart >/dev/null 2>&1
  wait_health
  # smoke runner 以退出码表达任务失败——不能让 set -e 中止门禁（否则后续腿
  # 和恢复重启都不执行）；记录结果，末尾聚合报错
  local rc=0
  "$PY" -m eval.smoke.runner --suite chain || rc=$?
  if [[ $rc != 0 ]]; then
    SMOKE_FAILURES+=("${backend}(exit=${rc})")
  fi
}

SMOKE_FAILURES=()

if [[ $SMOKE_ONLY == 0 ]]; then
  pytest_leg jsonl
  pytest_leg sqlite
fi
if [[ $RUN_SMOKE == 1 ]]; then
  smoke_leg jsonl
  if [[ $SMOKE_DUAL == 1 ]]; then
    smoke_leg sqlite
  fi
  echo ""
  echo "════════ 恢复默认后端服务 ════════"
  ./start.sh restart >/dev/null 2>&1
  wait_health
fi

echo ""
if [[ ${#SMOKE_FAILURES[@]} -gt 0 ]]; then
  echo "❌ smoke 有失败腿: ${SMOKE_FAILURES[*]}（报告见 eval/reports/）"
  exit 1
fi
echo "✅ 双后端门禁通过"
