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
SYNCREDMINE_SRC_DIR="$SCRIPT_DIR" SYNCREDMINE_INSTALL_DIR="$INSTALL_DIR" "$PYTHON_BIN" - <<'PY'
import os
import shutil

src = os.environ["SYNCREDMINE_SRC_DIR"]
dst = os.environ["SYNCREDMINE_INSTALL_DIR"]

skip_dirs = {'.git', '__pycache__', 'logs', '.idea', '.vscode', '.pytest_cache', '.mypy_cache', '.venv', 'venv'}
skip_files = {'.DS_Store'}
skip_suffixes = ('.pyc', '.pyo', '.swp', '.tmp', '~')
preserve_dirs = {'logs'}
desired_dirs = {''}
desired_files = set()

for root, dirs, files in os.walk(src):
    dirs[:] = [d for d in dirs if d not in skip_dirs]
    rel_root = os.path.relpath(root, src)
    rel_root = '' if rel_root == '.' else rel_root
    desired_dirs.add(rel_root)
    dst_root = dst if not rel_root else os.path.join(dst, rel_root)
    os.makedirs(dst_root, exist_ok=True)
    for name in files:
        if name in skip_files or name.endswith(skip_suffixes):
            continue
        rel_path = os.path.join(rel_root, name) if rel_root else name
        desired_files.add(rel_path)
        shutil.copy2(os.path.join(root, name), os.path.join(dst_root, name))

for root, dirs, files in os.walk(dst, topdown=False):
    rel_root = os.path.relpath(root, dst)
    rel_root = '' if rel_root == '.' else rel_root

    for name in files:
        rel_path = os.path.join(rel_root, name) if rel_root else name
        if rel_path in desired_files:
            continue
        if any(rel_path == keep or rel_path.startswith(keep + os.sep) for keep in preserve_dirs):
            continue
        os.remove(os.path.join(root, name))

    for name in dirs:
        rel_path = os.path.join(rel_root, name) if rel_root else name
        if rel_path in preserve_dirs or any(rel_path.startswith(keep + os.sep) for keep in preserve_dirs):
            continue
        if rel_path not in desired_dirs:
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
PY
chmod +x "$INSTALL_DIR/syncRedmine.py"
[[ -f "$INSTALL_DIR/install.sh" ]] && chmod +x "$INSTALL_DIR/install.sh"
[[ -f "$INSTALL_DIR/uninstall.sh" ]] && chmod +x "$INSTALL_DIR/uninstall.sh"
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
