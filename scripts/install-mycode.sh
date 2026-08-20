#!/usr/bin/env bash
# 安装 mycode 命令到系统 PATH。
# macOS 默认文件系统大小写不敏感，mycode 与 MyCode 指向同一文件，
# 因此只需一个符号链接，两种写法都能启动。
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$SCRIPT_DIR/mycode"
LINK_DIR="${INSTALL_DIR:-/usr/local/bin}"

chmod +x "$TARGET"

if [ ! -w "$LINK_DIR" ]; then
  echo "需要管理员权限写入 $LINK_DIR，请在终端手动执行："
  echo "  sudo ln -sf \"$TARGET\" \"$LINK_DIR/mycode\""
  exit 1
fi

ln -sf "$TARGET" "$LINK_DIR/mycode"
echo "已安装：$LINK_DIR/mycode -> $TARGET"
echo "现在可以在任意目录运行 mycode（或 MyCode）启动，工作区为当前文件夹。"
