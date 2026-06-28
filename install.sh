#!/bin/sh
# wiki-sync 安装脚本
# 把 wiki_sync.py 安装为 ~/.local/bin/wiki-sync

set -e

BIN_DIR="$HOME/.local/bin"
SCRIPT_URL="https://raw.githubusercontent.com/Eric-Ruan-jingwei/wiki-sync/main/wiki_sync.py"
TARGET="$BIN_DIR/wiki-sync"

mkdir -p "$BIN_DIR"

if [ -f "./wiki_sync.py" ]; then
    # 本地安装（在项目目录里运行）
    cp "./wiki_sync.py" "$TARGET"
else
    # 远程安装
    echo "正在下载 wiki-sync..."
    curl -fsSL "$SCRIPT_URL" -o "$TARGET"
fi

chmod +x "$TARGET"

echo "✅ 已安装到 $TARGET"
echo ""

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo "⚠ $BIN_DIR 不在你的 PATH 里。"
        echo "  请在 ~/.zshrc 中加入这一行，然后重开终端："
        echo ""
        echo '      export PATH="$HOME/.local/bin:$PATH"'
        echo ""
        ;;
esac

echo "用法："
echo "  wiki-sync list       列出可导入的对话"
echo "  wiki-sync import     导入最近一条对话"
echo "  wiki-sync --help     查看全部命令"
