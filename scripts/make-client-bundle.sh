#!/usr/bin/env bash
# 打「端侧」安装包：mycode-<VERSION>-client.tar.gz
#
# 端与云是同一条版本线、同一个 commit 出的两种产物：
#   云 = 镜像   （release-image.yml，每次 push 都出，SHA tag，ArgoCD 拉走）
#   端 = 这个包 （release-client.yml，只在 v* tag 时出，挂到 GitHub Release）
# 所以这份清单刻意与 Dockerfile 运行层的 COPY 对齐：两边差一个路径，就是
# 「仓库里跑得通、装上就 ImportError」这类只在客户端暴露的故障。
# 差异只有三类，都是"客户端用 mycode/start.sh 启动、容器直接跑 main.py"带来的：
# start.sh、scripts/pin-wsl.sh、requirements.txt + 两个安装脚本。
#
# 用法：
#   scripts/make-client-bundle.sh [输出目录]   打包（frontend/dist 必须已构建）
#   scripts/make-client-bundle.sh --check      只校验清单还在不在，不出包（CI 门禁）
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BUNDLE_PATHS=(
  agents
  frontend/server
  frontend/openapi.json
  frontend/dist
  mycode
  start.sh
  VERSION
  LICENSE
  requirements.txt
  scripts/install-client.sh
  scripts/install-mycode.sh
  scripts/pin-wsl.sh
)

# 构建产物，不进 Git。--check 跳过它（CI 的 backend/shell 作业不装 node），
# 真打包时缺了则明确报错，而不是让 tar 抛一句看不懂的 "Cannot stat"。
BUILD_ARTIFACT="frontend/dist"

fail_missing() {
  echo "❌ 安装包清单里有路径已不存在（改名/删除后没同步这份清单）：" >&2
  printf '   %s\n' "${@}" >&2
  exit 1
}

if [ "${1:-}" = "--check" ]; then
  missing=()
  for p in "${BUNDLE_PATHS[@]}"; do
    [ "$p" = "$BUILD_ARTIFACT" ] && continue
    [ -e "$REPO_ROOT/$p" ] || missing+=("$p")
  done
  if [ ${#missing[@]} -gt 0 ]; then
    fail_missing "${missing[@]}"
  fi
  echo "✓ 安装包清单 ${#BUNDLE_PATHS[@]} 条路径均在（$BUILD_ARTIFACT 是构建产物，跳过）"
  exit 0
fi

OUT_DIR="${1:-$REPO_ROOT/dist-release}"
VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")"
[ -n "$VERSION" ] || { echo "❌ VERSION 是空的" >&2; exit 1; }

missing=()
for p in "${BUNDLE_PATHS[@]}"; do
  [ -e "$REPO_ROOT/$p" ] || missing+=("$p")
done
if [ ${#missing[@]} -gt 0 ]; then
  if [ ${#missing[@]} -eq 1 ] && [ "${missing[0]}" = "$BUILD_ARTIFACT" ]; then
    echo "❌ 缺 $BUILD_ARTIFACT：先 (cd frontend && npm ci && npm run build)" >&2
    exit 1
  fi
  fail_missing "${missing[@]}"
fi

NAME="mycode-$VERSION"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/$NAME"
mkdir -p "$ROOT"

for p in "${BUNDLE_PATHS[@]}"; do
  mkdir -p "$ROOT/$(dirname "$p")"
  cp -R "$REPO_ROOT/$p" "$ROOT/$p"
done
# 开发机上跑过测试就会留下一地 __pycache__，别把它们发出去
find "$ROOT" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$ROOT" -name '*.pyc' -delete
chmod +x "$ROOT/mycode" "$ROOT/start.sh" "$ROOT"/scripts/*.sh

mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/$NAME-client.tar.gz"
rm -f "$OUT"
tar -czf "$OUT" -C "$STAGE" "$NAME"

echo "✓ $OUT"
echo "  版本 $VERSION，$(du -h "$OUT" | cut -f1)，$(tar -tzf "$OUT" | wc -l) 个条目"
echo "  装法：tar xzf $NAME-client.tar.gz && cd $NAME && ./scripts/install-client.sh"
