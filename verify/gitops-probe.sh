#!/usr/bin/env bash
# 证明 GitOps 真的在跑：自动同步、集群版本与 Git 一致、手改集群会被自愈。
#
# 为什么必须有这个脚本：ArgoCD 装上、Application 显示 Synced，都不等于「回滚真的能用」。
# 简历里那句「自动部署、支持一键回滚」要能当场重跑一遍，否则它只是装了个工具。
#
# 回滚（git revert）刻意**不**自动化：它要 push 到 GitHub，属于影响共享状态的动作，
# 得由人决定什么时候推。步骤见 deploy/README.md 第 4 节。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
mkdir -p verify/logs

export PATH="$HOME/.local/bin:$PATH"

NS=mycode
LOG="verify/logs/gitops-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "PASS: $1"; }

app_status() { kubectl -n argocd get application mycode -o jsonpath="$1"; }

echo "=== 断言 1：Application 处于 Synced + Healthy ==="
S=""; H=""
for i in $(seq 1 60); do
  S=$(app_status '{.status.sync.status}' 2>/dev/null || echo "")
  H=$(app_status '{.status.health.status}' 2>/dev/null || echo "")
  [ "$S" = "Synced" ] && [ "$H" = "Healthy" ] && break
  sleep 5
done
[ "$S" = "Synced" ] || fail "SYNC=$S（期望 Synced）"
[ "$H" = "Healthy" ] || fail "HEALTH=$H（期望 Healthy）"
pass "Synced + Healthy"

echo "=== 断言 2：集群跑的版本 == Git 里声明的版本 ==="
# 必须锚定行首的 4 空格缩进，不能用 `grep -oP 'newTag:\s*\K\S+'`：
# kustomization.yaml 第 8 行的**注释里**就写着 `^    newTag: `，那条 grep 会把注释一起
# 匹配进来，GIT_TAG 变成两行，比对必然失败（2026-10-23 实测踩到）。
# 用与 CI 回写完全相同的 sed 表达式，两边口径才对得上。
# 末尾的 `tr -d '\r'` 不是防御性代码：`.gitattributes` 是 `* text=auto`，
# Windows 检出的工作树里这个 yaml 是 **CRLF**（git 里存的是 LF），
# 于是 GIT_TAG 会带上一个 `\r`，和集群镜像名比对时**两个值打印出来看着完全一样却判 FAIL**
# （2026-10-23 实测）。CI 在 Linux runner 上跑，那里的文件是 LF，`tr` 是 no-op。
GIT_TAG=$(sed -n 's/^    newTag: //p' deploy/kind/kustomization.yaml | tr -d '\r')
[ -n "$GIT_TAG" ] || fail "从 deploy/kind/kustomization.yaml 解析不出 newTag"
RUNNING=$(kubectl -n "$NS" get deploy control-plane -o jsonpath='{.spec.template.spec.containers[0].image}')
echo "Git 声明 tag=$GIT_TAG   集群镜像=$RUNNING"
[[ "$RUNNING" == *":$GIT_TAG" ]] || fail "集群镜像与 Git 声明不一致（漂移）"
pass "Git 与集群一致（tag=$GIT_TAG）"

echo "=== 断言 3：手改集群会被 selfHeal 改回来 ==="
ORIG=$(kubectl -n "$NS" get deploy control-plane \
  -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}')
kubectl -n "$NS" patch deploy control-plane --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"2Gi"}]' >/dev/null
echo "已手动把 memory limit 从 $ORIG 改成 2Gi，等 selfHeal（默认 3 分钟轮询，手动触发一次加速）"
kubectl -n argocd patch application mycode --type=merge \
  -p '{"operation":{"initiatedBy":{"username":"probe"},"sync":{}}}' >/dev/null 2>&1 || true
NOW="2Gi"
for i in $(seq 1 40); do
  NOW=$(kubectl -n "$NS" get deploy control-plane \
    -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}')
  [ "$NOW" != "2Gi" ] && break
  sleep 5
done
[ "$NOW" != "2Gi" ] || fail "selfHeal 没把手改的 2Gi 改回 Git 声明值"
[ "$NOW" = "$ORIG" ] || echo "NOTE: 自愈成 $NOW，与改动前的 $ORIG 不同，人工核对一下"
pass "手改被自愈回 $NOW"

echo
echo "=== GitOps 三条断言全过 ==="
echo "证据日志：$LOG"
echo
echo "回滚实证需人工做（涉及 git revert 与 push），步骤："
echo "  1) PIN=\$(git log --oneline -20 | grep 'pin image to sha-' | head -1 | awk '{print \$1}')"
echo "     git revert --no-edit \"\$PIN\" && git push"
echo "  2) 等 ArgoCD 同步（最多 3 分钟轮询；急的话在 UI 点 Sync）："
echo "     watch -n 5 \"kubectl -n mycode get deploy control-plane -o jsonpath='{.spec.template.spec.containers[0].image}'; echo\""
echo "  3) 记录从 git push 到集群生效的秒数，填进台账 COMMIT_TO_DEPLOYED_SECONDS"
echo "  4) 验完再 git revert 那次 revert，把版本推回来"
