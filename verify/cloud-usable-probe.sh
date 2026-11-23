#!/usr/bin/env bash
# A 批「云端真能干活」的 12 条实证。全过才算验收通过。
#
# 与 deploy-probe.sh 的分工：那个验「后端搬进集群还活着」，这个验
# 「提一个任务 → 一次性沙箱里真跑 agent → 结果落地成 PR」这条链。
# 每条断言都对应 docs/superpowers/specs/2026-11-23-cloud-usable-a-design.md §8.2。
#
# 需要：kind 集群已起（mycode cloud-up）、控制面 Pod Ready、镜像里有 nodejs/npm。
# 断言 8 额外需要一个有 push 权限的 GitHub PAT；没有就如实记「未跑」，不算通过。
set -euo pipefail

# ⚠️ 只跟 localhost 说话，必须清空代理：宿主 HTTP_PROXY 会把 curl 劫到 Windows 侧，
# 那儿没有 8080，于是回一个带 Proxy-Connection 头的**假 502**，把排查带偏。
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export NO_PROXY='*' no_proxy='*'

# ⚠️ 凡是「非零退出码本身就是合法观测值」的取值，一律挂 `|| true`：
# `kubectl auth can-i` 答 no 时退出 1，jsonpath 取不到的字段也退出 1。
# 不挂的话 set -e 会在断言开口之前就把脚本掐死，日志里只留一个标题、
# 没有 FAIL 也没有原因——把探针自己的 bug 伪装成集群故障。判定权归断言。

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
mkdir -p verify/logs

NS=mycode
export API="${MYCODE_CLOUD_URL:-http://localhost:8080}"
SEL='app.kubernetes.io/component=control-plane'
LOG="verify/logs/cloud-usable-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "PASS: $1"; }
skip() { echo "未跑: $1"; }

# 访问令牌只进环境变量，绝不 echo。日志是要贴给别人看的。
TOKEN=$(kubectl -n "$NS" get secret mycode-secrets -o jsonpath='{.data.auth-token}' 2>/dev/null | base64 -d || true)
[ -n "$TOKEN" ] || fail "取不到 mycode-secrets/auth-token，先跑 scripts/k8s-secret.sh"
export TOKEN

# --- HTTP 帮手 ---------------------------------------------------------------
# 用 python 的 urllib 而不是 curl 拼 JSON：prompt 里有引号与换行，
# 手拼 shell 字符串迟早拼出一个语法错误或者更糟——注入。
api_get() {
  python3 - "$1" <<'PY'
import os, sys, urllib.request
req = urllib.request.Request(os.environ["API"] + sys.argv[1],
                             headers={"Authorization": "Bearer " + os.environ["TOKEN"]})
print(urllib.request.urlopen(req, timeout=60).read().decode())
PY
}

api_send() {   # method path json-body
  python3 - "$1" "$2" "$3" <<'PY'
import os, sys, urllib.request
method, path, body = sys.argv[1], sys.argv[2], sys.argv[3]
data = body.encode() if body else None
req = urllib.request.Request(os.environ["API"] + path, data=data, method=method,
                             headers={"Authorization": "Bearer " + os.environ["TOKEN"],
                                      "Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=120).read().decode())
PY
}

field() { python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1]) or "")' "$1"; }

# rollout status 绿了不等于请求能进来：ingress 的 endpoint 还要滞后一两秒，
# 这期间提交任务会拿到 503。等条件，不等运气。
wait_api() {
  for _ in $(seq 1 60); do
    if python3 - >/dev/null 2>&1 <<'PY'
import os, urllib.request
urllib.request.urlopen(os.environ["API"] + "/api/health", timeout=5)
PY
    then return 0; fi
    sleep 2
  done
  return 1
}

submit() {     # repo prompt [setup]
  local payload
  payload=$(python3 - "$1" "$2" "${3:-}" <<'PY'
import json, sys
print(json.dumps({"repo_url": sys.argv[1], "prompt": sys.argv[2], "setup_command": sys.argv[3]}))
PY
)
  api_send POST /api/cloud/tasks "$payload" | field id
}

task_status() { api_get "/api/cloud/tasks/$1" | field status; }

wait_terminal() {   # id timeout_s
  local id="$1" budget="$2" s=""
  for _ in $(seq 1 "$budget"); do
    s=$(task_status "$id")
    case "$s" in succeeded|failed|timeout|cancelled) echo "$s"; return 0 ;; esac
    sleep 2
  done
  echo "$s"
  return 1
}

sandbox_pod() {
  kubectl -n "$NS" get pod -l "mycode.cloud/task-id=$1" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true
}

REPO="${PROBE_REPO:-https://github.com/soft-zihan/MyCode}"
# 大部分断言刻意用「不改文件」的 prompt：没有改动就不需要 PAT 也能走完落地，
# 于是 PAT 只影响断言 8 一条，其余 11 条在任何机器上都能真跑。
NOOP='只回一句「probe ok」就结束，不要读写任何文件，不要跑任何命令。'

echo "=== 断言 1：控制面 SA 能建 Job ==="
CAN=$(kubectl auth can-i create jobs --as=system:serviceaccount:${NS}:mycode-control-plane -n "$NS" || true)
[ "$CAN" = "yes" ] || fail "控制面 SA 建不了 Job（$CAN）——deploy/base/rbac.yaml 没同步？"
pass "控制面 SA create jobs = yes"

echo
echo "=== 断言 2：同一 SA 读不到 Secret、改不了 Deployment ==="
S=$(kubectl auth can-i get secrets --as=system:serviceaccount:${NS}:mycode-control-plane -n "$NS" || true)
D=$(kubectl auth can-i create deployments --as=system:serviceaccount:${NS}:mycode-control-plane -n "$NS" || true)
[ "$S" = "no" ] || fail "控制面能读 Secret（$S）——最小权限破了"
[ "$D" = "no" ] || fail "控制面能建 Deployment（$D）——最小权限破了"
pass "get secrets = no，create deployments = no（只能管 Job 与读 Pod/日志）"

echo
echo "=== 断言 3：提一个任务，一次性 Job 真跑完 ==="
kubectl -n "$NS" wait --for=condition=Ready pod -l "$SEL" --timeout=180s >/dev/null || fail "控制面 Pod 未 Ready"
wait_api || fail "控制面 Ready 了但 /api/health 120s 内没通"
T3=$(submit "$REPO" "$NOOP")
[ -n "$T3" ] || fail "提任务没返回 id"
echo "任务 id：$T3"
JOB="mycode-task-$T3"
for _ in $(seq 1 60); do
  kubectl -n "$NS" get job "$JOB" >/dev/null 2>&1 && break
  sleep 2
done
kubectl -n "$NS" get job "$JOB" >/dev/null 2>&1 || fail "60s 内没出现 Job $JOB"
S3=$(wait_terminal "$T3" 150) || fail "任务未在 300s 内到终态（最后状态 $S3）"
[ "$S3" = "succeeded" ] || fail "任务终态是 $S3：$(api_get "/api/cloud/tasks/$T3" | field error)"
JS=$(kubectl -n "$NS" get job "$JOB" -o jsonpath='{.status.succeeded}' || true)
[ "$JS" = "1" ] || fail "Job 没有 succeeded=1（拿到 '$JS'）"
pass "任务 $T3 → Job $JOB succeeded=1，任务状态 succeeded"

echo
echo "=== 断言 4：沙箱里 node/npm 真能装包并跑起来（工具链自证）==="
# 装在 /tmp（emptyDir）而不是仓库里：不污染工作区，也就不会凭空产生「有改动要落地」。
# 括号是必须的：setup 里 cd 出去若不套子 shell，agent 的工作目录就跟着变了。
SETUP4="(cd /tmp && npm install --silent left-pad@1.3.0 && node -e 'console.log(\"TOOLCHAIN_OK\", require(\"left-pad/package.json\").version)')"
T4=$(submit "$REPO" "$NOOP" "$SETUP4")
S4=$(wait_terminal "$T4" 180) || fail "工具链任务未在 360s 内到终态（$S4）"
[ "$S4" = "succeeded" ] || fail "工具链任务终态 $S4：$(api_get "/api/cloud/tasks/$T4" | field error)"
LOG4=$(kubectl -n "$NS" logs "job/mycode-task-$T4" --tail=300 2>/dev/null || true)
echo "$LOG4" | grep -q "TOOLCHAIN_OK 1.3.0" \
  || fail "Job 日志里没有 'TOOLCHAIN_OK 1.3.0'——npm/node 在沙箱里没真跑通"
pass "沙箱内 npm install left-pad@1.3.0 + node 打印版本成功（日志有 TOOLCHAIN_OK 1.3.0）"

echo
echo "=== 断言 5：只读根与非 root 没有为了装包而降档 ==="
ROR=$(kubectl -n "$NS" get job "mycode-task-$T4" \
  -o jsonpath='{.spec.template.spec.containers[0].securityContext.readOnlyRootFilesystem}' || true)
NR=$(kubectl -n "$NS" get job "mycode-task-$T4" -o jsonpath='{.spec.template.spec.securityContext.runAsNonRoot}' || true)
UID_=$(kubectl -n "$NS" get job "mycode-task-$T4" -o jsonpath='{.spec.template.spec.securityContext.runAsUser}' || true)
[ "$ROR" = "true" ] || fail "沙箱 readOnlyRootFilesystem=$ROR"
[ "$NR" = "true" ] || fail "沙箱 runAsNonRoot=$NR"
[ "$UID_" = "10001" ] || fail "沙箱 runAsUser=$UID_"
pass "readOnlyRootFilesystem=true、runAsNonRoot=true、runAsUser=10001（装包靠 HOME 可写，不是放开只读根）"

echo
echo "=== 断言 9：并发上限——第二个任务必须排队 ==="
P=$(submit "$REPO" "$NOOP" "sleep 60")
Q=$(submit "$REPO" "$NOOP" "sleep 5")
for _ in $(seq 1 45); do
  [ "$(task_status "$P")" = "running" ] && break
  sleep 2
done
SP=$(task_status "$P"); SQ=$(task_status "$Q")
echo "第一个 $SP，第二个 $SQ"
[ "$SP" = "running" ] || fail "第一个任务没进 running（$SP）"
[ "$SQ" = "queued" ] || fail "并发上限 1 时第二个任务是 $SQ，不该被放行"
pass "并发上限生效：P running 时 Q 停在 queued"

echo
echo "=== 断言 6：沙箱里没有 Git PAT，也看不到别的任务 ==="
POD=$(sandbox_pod "$P")
[ -n "$POD" ] || fail "取不到 P 的沙箱 Pod"
LEAK=$(kubectl -n "$NS" exec "$POD" -- sh -c 'env | grep -E "GITHUB_TOKEN|GH_TOKEN|GIT_ASKPASS|MYCODE_GIT_TOKEN" || true')
[ -z "$LEAK" ] || fail "沙箱 env 里有 Git 凭据字段"
LS6=$(kubectl -n "$NS" exec "$POD" -- sh -c 'ls -a /home/app/.mycode' || true)
echo "沙箱看到的 MYCODE_HOME：$(echo "$LS6" | tr '\n' ' ')"
echo "$LS6" | grep -q "cloud_git_token" && fail "沙箱能看到 PVC 根的 cloud_git_token"
echo "$LS6" | grep -q "cloud_tasks" && fail "沙箱能看到 cloud_tasks 目录（说明挂的是 PVC 根）"
echo "$LS6" | grep -q "$T3" && fail "沙箱能看到别的任务目录 $T3"
pass "env 无 Git 凭据；MYCODE_HOME 里没有 cloud_git_token、没有 cloud_tasks、没有别的任务"

echo
echo "=== 断言 7：出网白名单——白名单外拒、白名单内通 ==="
# 镜像里没装 curl（刻意只装运行期依赖），用 python 的 urllib 做同一件事。
if kubectl -n "$NS" exec "$POD" -- python -c \
  "import urllib.request; urllib.request.urlopen('https://example.com', timeout=8)" >/dev/null 2>&1; then
  fail "沙箱能访问白名单外的 example.com——CiliumNetworkPolicy 没生效"
fi
IN=$(kubectl -n "$NS" exec "$POD" -- python -c \
  "import urllib.request; print(urllib.request.urlopen('https://pypi.org/simple/six/', timeout=15).status)" 2>/dev/null || true)
[ "$IN" = "200" ] || fail "沙箱访问白名单内的 pypi.org 失败（拿到 '$IN'）"
pass "example.com 被拒；pypi.org/simple/six/ 返回 200"

echo
echo "=== 断言 9（续）：第一个终态后第二个才起 ==="
SP=$(wait_terminal "$P" 120) || fail "P 未在 240s 内到终态（$SP）"
for _ in $(seq 1 45); do
  [ "$(task_status "$Q")" != "queued" ] && break
  sleep 2
done
SQ=$(task_status "$Q")
[ "$SQ" != "queued" ] || fail "P 已终态，Q 还停在 queued——准入没放行"
pass "P 终态（$SP）后 Q 被放行（$SQ）"

echo
echo "=== 断言 11：取消真删 Job ==="
for _ in $(seq 1 45); do
  [ "$(task_status "$Q")" = "running" ] && break
  sleep 2
done
api_send DELETE "/api/cloud/tasks/$Q" "" >/dev/null
SQ=$(task_status "$Q")
[ "$SQ" = "cancelled" ] || fail "取消后状态是 $SQ"
for _ in $(seq 1 30); do
  kubectl -n "$NS" get job "mycode-task-$Q" >/dev/null 2>&1 || break
  sleep 2
done
kubectl -n "$NS" get job "mycode-task-$Q" >/dev/null 2>&1 && fail "取消后 Job 还在集群里"
pass "DELETE → 状态 cancelled，Job mycode-task-$Q 已从集群消失"

echo
echo "=== 断言 10：超时被识别，且仍然走完落地流程 ==="
# 改 Deployment 的 deadline 会被 ArgoCD selfHeal 秒回滚，所以临时关自动同步，
# 验完连同 env 一起还回去（trap 兜底：中途 fail 也要还）。
ARGO_APP=""
ARGO_AUTO="null"
if kubectl -n argocd get application mycode >/dev/null 2>&1; then
  ARGO_APP=mycode
  # 原样还回去，别无条件开 selfHeal：拿本地镜像做实证时，开回来就把镜像换成 Git 里的
  # tag，下一轮探针跑的是旧代码，看起来像「修复没生效」。
  ARGO_AUTO=$(kubectl -n argocd get application mycode -o json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["spec"].get("syncPolicy",{}).get("automated")))' 2>/dev/null || true)
  ARGO_AUTO=${ARGO_AUTO:-null}
fi
restore_deadline() {
  kubectl -n "$NS" set env deploy/control-plane MYCODE_SANDBOX_DEADLINE=1800 >/dev/null 2>&1 || true
  if [ -n "$ARGO_APP" ]; then
    kubectl -n argocd patch application "$ARGO_APP" --type=merge \
      -p "{\"spec\":{\"syncPolicy\":{\"automated\":$ARGO_AUTO}}}" >/dev/null 2>&1 || true
  fi
}
trap restore_deadline EXIT
if [ -n "$ARGO_APP" ]; then
  kubectl -n argocd patch application "$ARGO_APP" --type=merge \
    -p '{"spec":{"syncPolicy":{"automated":null}}}' >/dev/null
fi
kubectl -n "$NS" set env deploy/control-plane MYCODE_SANDBOX_DEADLINE=30 >/dev/null
kubectl -n "$NS" rollout status deploy/control-plane --timeout=180s >/dev/null || fail "改 deadline 后 Pod 没起来"
wait_api || fail "改 deadline 后 /api/health 120s 内没通"
T10=$(submit "$REPO" "$NOOP" "sleep 120")
S10=$(wait_terminal "$T10" 90) || fail "超时任务未在 180s 内到终态（$S10）"
[ "$S10" = "timeout" ] || fail "deadline 30s 的任务终态是 $S10，应为 timeout"
# 按 reason 取，不按 type=Failed：1.31+ 先写 FailureTarget，等 Pod 收干净才写 Failed，
# 实测差 31s，而任务在这之前就到终态了。只认 Failed 会取到空串。
REASON=$(kubectl -n "$NS" get job "mycode-task-$T10" \
  -o jsonpath='{.status.conditions[?(@.reason=="DeadlineExceeded")].reason}' 2>/dev/null || true)
[ "$REASON" = "DeadlineExceeded" ] || fail "Job 的失败原因不是 DeadlineExceeded（$REASON）"
pass "activeDeadlineSeconds=30 → Job DeadlineExceeded，任务经落地流程后状态 timeout"
restore_deadline
trap - EXIT
kubectl -n "$NS" rollout status deploy/control-plane --timeout=180s >/dev/null
wait_api || fail "还原 deadline 后 /api/health 120s 内没通"

echo
echo "=== 断言 8：结果落地成 GitHub 上真实存在的 PR ==="
PAT_CONFIGURED=$(api_get /api/cloud/git-token | field configured)
if [ "$PAT_CONFIGURED" != "True" ]; then
  skip "断言 8：未配置 GitHub PAT（mycode cloud 的 PUT /api/cloud/git-token），无法验 PR 落地"
else
  T8=$(submit "$REPO" "在仓库根新建文件 cloud-probe.txt，内容写一行 probe-ok，然后结束。不要做别的事。")
  S8=$(wait_terminal "$T8" 180) || fail "落地任务未在 360s 内到终态（$S8）"
  [ "$S8" = "succeeded" ] || fail "落地任务终态 $S8：$(api_get "/api/cloud/tasks/$T8" | field error)"
  PR=$(api_get "/api/cloud/tasks/$T8" | field pr_url)
  [ -n "$PR" ] || fail "任务 succeeded 但 pr_url 为空"
  PR_API=$(printf '%s' "$PR" | sed -E 's#https://github.com/([^/]+)/([^/]+)/pull/([0-9]+)#https://api.github.com/repos/\1/\2/pulls/\3#')
  CODE=$(python3 - "$PR_API" <<'PY'
import sys, urllib.request
try:
    print(urllib.request.urlopen(urllib.request.Request(sys.argv[1],
          headers={"Accept": "application/vnd.github+json"}), timeout=20).status)
except Exception as e:
    print(getattr(e, "code", "000"))
PY
)
  [ "$CODE" = "200" ] || fail "GitHub API 查不到这个 PR（HTTP $CODE）：$PR"
  pass "pr_url 非空且 GitHub API 返回 200：$PR"
fi

echo
echo "=== 断言 12：prune 只清终态，PVC 占用真的下降 ==="
BEFORE=$(kubectl -n "$NS" exec deploy/control-plane -- du -sm /home/app/.mycode/workspaces 2>/dev/null | cut -f1 || true)
BEFORE="${BEFORE:-0}"
PRUNE=$(api_send POST /api/cloud/prune "")
N=$(printf '%s' "$PRUNE" | field pruned)
AFTER=$(kubectl -n "$NS" exec deploy/control-plane -- du -sm /home/app/.mycode/workspaces 2>/dev/null | cut -f1 || true)
AFTER="${AFTER:-0}"
echo "工作区占用：${BEFORE} MiB → ${AFTER} MiB，清理 $N 个"
[ "$N" -ge 1 ] || fail "prune 一个都没清（$PRUNE）"
[ "$AFTER" -lt "$BEFORE" ] || fail "prune 后 PVC 占用没下降（$BEFORE → $AFTER）"
LEFT=$(kubectl -n "$NS" exec deploy/control-plane -- sh -c 'ls /home/app/.mycode/workspaces 2>/dev/null || true')
for id in "$T3" "$T4" "$P"; do
  printf '%s' "$LEFT" | grep -q -- "--$id" && fail "终态任务 $id 的工作区还在"
done
pass "prune 清掉 $N 个终态工作区，占用 ${BEFORE}→${AFTER} MiB"

echo
echo "=== A 批验收结束 ==="
echo "证据日志：$LOG"