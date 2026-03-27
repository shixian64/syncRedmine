#!/bin/bash
# syncRedmine Ubuntu 安装脚本
# 将程序安装为开机自启动的后台服务（系统托盘）

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/.local/share/syncRedmine"
LOG_DIR="$INSTALL_DIR/logs"
AUTOSTART_DIR="$HOME/.config/autostart"
DESKTOP_FILE="$AUTOSTART_DIR/syncRedmine.desktop"
PYTHON_BIN="${PYTHON_BIN:-python3}"

ensure_modern_pip() {
    local pip_version major

    if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
        echo "      错误：未检测到可用的 pip，请先安装 python3-pip"
        exit 1
    fi

    pip_version="$("$PYTHON_BIN" -m pip --version | awk '{print $2}')"
    major="${pip_version%%.*}"

    if [[ "${major:-0}" -lt 21 ]]; then
        echo "      检测到 pip 版本较旧 ($pip_version)，升级用户侧 pip 以兼容 PyQt5 wheel..."
        "$PYTHON_BIN" -m pip install --user --upgrade pip --quiet
    fi
}

install_python_deps() {
    if ! "$PYTHON_BIN" -m pip install --prefer-binary -r "$SCRIPT_DIR/requirements.txt" --quiet; then
        echo "      Python 依赖安装失败。"
        echo "      可尝试以下方式后重试："
        echo "        1) 手动升级 pip: $PYTHON_BIN -m pip install --user --upgrade pip"
        echo "        2) Ubuntu 安装系统依赖: sudo apt install python3-pyqt5 python3-requests"
        exit 1
    fi
}

echo "======================================"
echo "  syncRedmine 安装程序"
echo "======================================"

# ── 安装 Python 依赖 ───────────────────────────────────────────────────────────
echo "[1/3] 安装 Python 依赖..."
ensure_modern_pip
install_python_deps
echo "      依赖安装完成"

# ── 复制程序文件 ───────────────────────────────────────────────────────────────
echo "[2/3] 安装程序文件到 $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$LOG_DIR"
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
echo "  日志目录: $LOG_DIR"
echo "  日志策略: 按天滚动，保留最近 3 天"
echo "  自启动:   $DESKTOP_FILE"
echo ""
echo "  立即启动: python3 $INSTALL_DIR/syncRedmine.py &"
echo "  打开设置: python3 $INSTALL_DIR/syncRedmine.py --setup"
echo ""
echo "  首次启动后请先完成设置。"
echo ""

# 询问是否立即启动（AUTO_INSTALL=1 时跳过，由调用方负责重启）
if [[ -z "${AUTO_INSTALL:-}" ]]; then
    read -p "是否立即启动 syncRedmine？[Y/n] " ans
    ans=${ans:-Y}
    if [[ "${ans,,}" == "y" ]]; then
        python3 "$INSTALL_DIR/syncRedmine.py" &
        echo "  syncRedmine 已在后台启动，请查看系统托盘图标。"
    fi
fi
