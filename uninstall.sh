#!/bin/bash
# syncRedmine 卸载脚本

INSTALL_DIR="$HOME/.local/share/syncRedmine"
LOG_DIR="$INSTALL_DIR/logs"
DESKTOP_FILE="$HOME/.config/autostart/syncRedmine.desktop"
CONFIG_FILE="$HOME/.commit_tool/sync_config.json"

echo "======================================"
echo "  syncRedmine 卸载程序"
echo "======================================"
echo ""

# ── 停止运行中的进程 ──────────────────────────────────────────────────────────
echo "[1/3] 停止 syncRedmine 进程..."
pkill -f "syncRedmine.py" 2>/dev/null && echo "      进程已停止" || echo "      无运行中的进程"

# ── 删除程序文件和自启动 ──────────────────────────────────────────────────────
echo "[2/3] 删除程序文件..."

if [ -d "$INSTALL_DIR" ]; then
    if [ -d "$LOG_DIR" ]; then
        echo "      检测到运行日志目录: $LOG_DIR"
    fi
    rm -rf "$INSTALL_DIR"
    echo "      已删除: $INSTALL_DIR (包含运行日志目录)"
else
    echo "      未找到: $INSTALL_DIR (跳过)"
fi

if [ -f "$DESKTOP_FILE" ]; then
    rm -f "$DESKTOP_FILE"
    echo "      已删除: $DESKTOP_FILE"
else
    echo "      未找到: $DESKTOP_FILE (跳过)"
fi

# ── 询问是否删除配置 ──────────────────────────────────────────────────────────
echo "[3/3] 处理配置文件..."

if [ -f "$CONFIG_FILE" ]; then
    read -p "      是否删除账号配置 ($CONFIG_FILE)？[y/N] " ans
    ans=${ans:-N}
    if [[ "${ans,,}" == "y" ]]; then
        rm -f "$CONFIG_FILE"
        echo "      配置已删除"
    else
        echo "      配置已保留"
    fi
else
    echo "      无配置文件 (跳过)"
fi

echo ""
echo "======================================"
echo "  卸载完成"
echo "======================================"
