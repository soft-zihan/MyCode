#!/usr/bin/env bash
# 从本地配置生成 K8s Secret。含明文 api_key，所以本脚本的产物绝不入库。
# 换机器 / 重建集群都要重跑一次。
set -euo pipefail

NS=mycode
SRC="${MYCODE_CONFIG_SRC:-$HOME/.my-code/config.json}"

[ -f "$SRC" ] || {
  echo "找不到 $SRC"
  echo "用 MYCODE_CONFIG_SRC=<路径> 指定别的 config.json"
  exit 1
}

command -v kubectl >/dev/null || { echo "缺 kubectl"; exit 1; }
kubectl get ns "$NS" >/dev/null 2>&1 || {
  echo "namespace $NS 不存在，先跑：kubectl apply -k deploy/base"
  exit 1
}

# config.json 整份走 Secret 而不是 ConfigMap：ModelEndpointConfig.api_key 内联在
# 同一份 JSON 里（agents/config.py），拆不开；放 ConfigMap 等于把密钥明文摊在集群里。
kubectl -n "$NS" create secret generic mycode-config \
  --from-file=config.json="$SRC" \
  --dry-run=client -o yaml | kubectl apply -f -

# 访问令牌单独一个 Secret：它以 env 注入，不该和 config.json 混在同一个只读卷里。
if [ -n "${MYCODE_AUTH_TOKEN:-}" ]; then
  TOKEN="$MYCODE_AUTH_TOKEN"
else
  TOKEN=$(openssl rand -hex 32)
fi
kubectl -n "$NS" create secret generic mycode-secrets \
  --from-literal=auth-token="$TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secret 已创建：mycode-config（挂载到 /etc/mycode）、mycode-secrets（env 注入 auth-token）"
echo "访问令牌：$TOKEN"
echo
echo "注意：K8s Secret 只是 base64，不是加密。别把它导出后提交，也别贴进聊天。"
