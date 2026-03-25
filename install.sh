#!/bin/bash
# syncRedmine Ubuntu 安装脚本
# 将程序安装为开机自启动的后台服务（系统托盘）

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/.local/share/syncRedmine"
AUTOSTART_DIR="$HOME/.config/autostart"
DESKTOP_FILE="$AUTOSTART_DIR/syncRedmine.desktop"

echo "======================================"
echo "  syncRedmine 安装程序"
echo "======================================"

# ── 安装 Python 依赖 ───────────────────────────────────────────────────────────
echo "[1/3] 安装 Python 依赖..."
pip3 install -r "$SCRIPT_DIR/requirements.txt" --quiet
echo "      依赖安装完成"

# ── 复制程序文件 ───────────────────────────────────────────────────────────────
echo "[2/3] 安装程序文件到 $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/syncRedmine.py" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/syncRedmine.py"
echo "      文件复制完成"

# ── 创建开机自启动 .desktop ────────────────────────────────────────────────────
echo "[3/3] 配置开机自启动..."
mkdir -p "$AUTOSTART_DIR"
cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Type=Application
Name=syncRedmine
Comment=提交后自动同步信息到Redmine（后台托盘）
Exec=python3 $INSTALL_DIR/syncRedmine.py
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
StartupNotify=false
EOF
echo "      自启动配置完成: $DESKTOP_FILE"

# ── 汇总 ──────────────────────────────────────────────────────────────────────
echo ""
echo "======================================"
echo "  安装完成！"
echo "======================================"
echo ""
echo "  程序位置: $INSTALL_DIR/syncRedmine.py"
echo "  自启动:   $DESKTOP_FILE"
echo ""
echo "  立即启动: python3 $INSTALL_DIR/syncRedmine.py &"
echo "  配置账号: python3 $INSTALL_DIR/syncRedmine.py --setup"
echo ""
echo "  首次启动后请配置 Gerrit 和 Redmine 账号。"
echo ""

# 询问是否立即启动
read -p "是否立即启动 syncRedmine？[Y/n] " ans
ans=${ans:-Y}
if [[ "${ans,,}" == "y" ]]; then
    python3 "$INSTALL_DIR/syncRedmine.py" &
    echo "  syncRedmine 已在后台启动，请查看系统托盘图标。"
fi
