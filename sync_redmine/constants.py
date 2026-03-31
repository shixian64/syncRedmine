# -*- coding: utf-8 -*-
"""常量、样式表与日志初始化。"""

import os, re, glob, logging
from logging.handlers import TimedRotatingFileHandler

# ═══════════════════════════════════════════════════════════════════════════════
# 路径与常量
# ═══════════════════════════════════════════════════════════════════════════════
COMMIT_TOOL_DIR = os.path.join(os.path.expanduser('~'), '.commit_tool')
CONFIG_FILE     = os.path.join(COMMIT_TOOL_DIR, 'sync_config.json')
DEFAULT_LOG     = os.path.join(os.path.expanduser('~'), 'commit_data.log')
APP_DATA_DIR    = os.path.join(os.path.expanduser('~'), '.local', 'share', 'syncRedmine')
LOG_DIR         = os.path.join(APP_DATA_DIR, 'logs')
LOG_FILE        = os.path.join(LOG_DIR, 'syncRedmine.log')
PLACEHOLDER     = '请填写'
POLL_INTERVAL   = 4          # 轮询间隔 (秒)
POLL_TIMEOUT    = 180        # 最长等待 push 时间 (秒)
RECENT_PUSH_SLACK = 8        # Gerrit 时间与本机时间允许偏差 (秒)
LOG_RETENTION_DAYS = 3       # 最多保留最近 3 个日志文件（通常对应当天 + 前 2 天）
AUTO_UPDATE_HOUR  = 10
AUTO_UPDATE_MINUTE = 0
GITHUB_DEFAULT_REPO = 'shixian64/syncRedmine'
GITHUB_DEFAULT_BRANCH = 'main'
VERSION_FILE = os.path.join(APP_DATA_DIR, '.current_version')

# commit_data.log 字段 → Redmine 自定义字段名
FIELD_MAP = {
    'Root Cause':      '【问题根源】',
    'Solution':        '【修复方案】',
    'Test_Report':     '【自测情况】',
    'Test_Suggestion': '【建议】',
    'Comment':         '【查找问题的思路】',
}
FIX_FIELD_NAME    = '【修复情况】'
SOLVER_FIELD_NAME = '解决者'

# ═══════════════════════════════════════════════════════════════════════════════
# 样式表
# ═══════════════════════════════════════════════════════════════════════════════
APP_STYLE_SHEET = """
QWidget {
    font-family: "Noto Sans CJK SC", "Microsoft YaHei", "PingFang SC", sans-serif;
    color: #0f172a;
}
QDialog {
    background: #eef4fb;
}
#DialogShell {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 22px;
}
#SectionPanel {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 16px;
}
#SectionTitle {
    font-size: 14pt;
    font-weight: 800;
    color: #0f172a;
}
#SectionDesc {
    font-size: 9pt;
    color: #64748b;
    line-height: 1.4em;
}
#FieldCaption {
    font-size: 9pt;
    font-weight: 700;
    color: #334155;
}
#FieldHint {
    font-size: 8pt;
    color: #94a3b8;
    line-height: 1.3em;
}
QLineEdit {
    border: 1px solid #cbd5e1;
    border-radius: 10px;
    padding: 0 14px;
    font-size: 10pt;
    background: #ffffff;
    selection-background-color: #bfdbfe;
}
QLineEdit:focus {
    border-color: #2563eb;
}
QComboBox {
    border: 1px solid #cbd5e1;
    border-radius: 10px;
    padding: 0 14px;
    font-size: 10pt;
    background: #ffffff;
    min-height: 44px;
}
QComboBox:focus, QComboBox:on {
    border-color: #2563eb;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: right center;
    width: 32px;
    border: none;
}
QComboBox QAbstractItemView {
    background: #f8fafc;
    border: 1px solid #cbd5e1;
    border-radius: 10px;
    selection-background-color: #dbeafe;
    selection-color: #1d4ed8;
    padding: 4px;
    outline: 0;
}
QPlainTextEdit {
    border: 1px solid #cbd5e1;
    border-radius: 10px;
    padding: 10px 14px;
    font-size: 10pt;
    background: #ffffff;
    selection-background-color: #bfdbfe;
}
QPlainTextEdit:focus {
    border-color: #2563eb;
}
#ErrorDetail {
    background: #fef2f2;
    border: 1px solid #fecaca;
    border-radius: 10px;
    color: #991b1b;
    font-size: 9pt;
}
#ValueText {
    font-size: 10pt;
    color: #1e293b;
    line-height: 1.45em;
}
#StatusText {
    font-size: 10pt;
    line-height: 1.45em;
}
#Divider {
    background: #e2e8f0;
    max-height: 1px;
    min-height: 1px;
}
#InfoStrip {
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
    border-radius: 14px;
}
#InlineInfoBlock {
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
}
#InlineState {
    font-size: 10pt;
    font-weight: 700;
    color: #1e293b;
}
#InlineSummary {
    font-size: 9pt;
    color: #64748b;
    line-height: 1.35em;
}
#MetaText {
    font-size: 9pt;
    color: #64748b;
    line-height: 1.35em;
}
#HeroEyebrow {
    font-size: 9pt;
    font-weight: 700;
    color: rgba(255,255,255,0.62);
    letter-spacing: 1px;
}
#HeroTitle {
    font-size: 17pt;
    font-weight: 900;
    color: #ffffff;
}
#HeroText {
    font-size: 9.5pt;
    color: rgba(255,255,255,0.78);
    line-height: 1.4em;
}
#PrimaryButton {
    background: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 #3b82f6, stop:1 #2563eb
    );
    color: #ffffff;
    border: none;
    border-radius: 12px;
    padding: 12px 32px;
    font-size: 10pt;
    font-weight: 700;
}
#PrimaryButton:hover {
    background: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 #60a5fa, stop:1 #3b82f6
    );
}
#PrimaryButton:pressed {
    background: #1d4ed8;
}
#PrimaryButton:disabled {
    background: #94a3b8;
}
#SecondaryButton {
    background: #ffffff;
    color: #334155;
    border: 1px solid #cbd5e1;
    border-radius: 12px;
    padding: 12px 24px;
    font-size: 10pt;
    font-weight: 600;
}
#SecondaryButton:hover {
    background: #f1f5f9;
    border-color: #94a3b8;
}
#GhostButton {
    background: transparent;
    color: #2563eb;
    border: none;
    padding: 6px 12px;
    font-size: 9pt;
    font-weight: 700;
}
#GhostButton:hover {
    color: #1d4ed8;
    text-decoration: underline;
}
#GhostButton:disabled {
    color: #94a3b8;
}
#LinkButton {
    background: transparent;
    color: #64748b;
    border: none;
    padding: 8px 16px;
    font-size: 9pt;
    font-weight: 600;
}
#LinkButton:hover {
    color: #2563eb;
    text-decoration: underline;
}
QCheckBox {
    font-size: 10pt;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 20px;
    height: 20px;
    border-radius: 6px;
    border: 2px solid #94a3b8;
    background: #ffffff;
}
QCheckBox::indicator:checked {
    background: #2563eb;
    border-color: #2563eb;
}
#FieldReveal {
    background: transparent;
    border: none;
}
QMenu {
    background: #1e293b;
    color: #e2e8f0;
    border-radius: 10px;
    padding: 6px 0;
}
QMenu::item {
    padding: 8px 18px;
    margin: 2px 0;
    border-radius: 10px;
}
QMenu::item:selected {
    background: #1d4ed8;
}
QMenu::separator {
    height: 1px;
    background: #1e293b;
    margin: 6px 8px;
}
QMessageBox {
    background: #f8fafc;
}
"""


# ═══════════════════════════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════════════════════════
def cleanup_old_logs():
    """最多保留最近 3 个日志文件（当前日志 + 最近 2 个滚动日志）。"""
    files = [p for p in glob.glob(os.path.join(LOG_DIR, 'syncRedmine.log*'))
             if os.path.isfile(p)]
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for old_file in files[LOG_RETENTION_DAYS:]:
        try:
            os.remove(old_file)
        except OSError:
            pass


def setup_logging():
    _logger = logging.getLogger('syncRedmine')
    if _logger.handlers:
        return _logger

    _logger.setLevel(logging.INFO)
    _logger.propagate = False

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        cleanup_old_logs()

        handler = TimedRotatingFileHandler(
            LOG_FILE,
            when='midnight',
            interval=1,
            backupCount=max(LOG_RETENTION_DAYS - 1, 0),
            encoding='utf-8',
            delay=True,
        )
        handler.suffix = "%Y-%m-%d"
        handler.extMatch = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(threadName)s %(message)s'))
        _logger.addHandler(handler)
        _logger.info("日志系统已启动，日志文件: %s，保留天数: %s", LOG_FILE, LOG_RETENTION_DAYS)
    except Exception as e:
        _logger.addHandler(logging.NullHandler())
        import sys
        print(f"[syncRedmine] 初始化日志失败: {e}", file=sys.stderr)

    return _logger


logger = setup_logging()
