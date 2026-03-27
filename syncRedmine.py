#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
syncRedmine - 独立后台程序
无需修改 commit_tool，自动检测 git push 完成后弹窗询问是否同步到 Redmine。

检测原理:
  1. 监控 ~/commit_data.log 变化 (commit 工具写入此文件)
  2. 用 Bug number / Topic ID 记录 Gerrit 上关联 topic 的 change 基线
  3. 轮询 Gerrit REST API, 发现新 change / patchset 更新 / 最近更新时间命中 → push 完成
  4. 弹出同步确认框

启动: python3 syncRedmine.py
配置: python3 syncRedmine.py --setup
"""

import sys, os, re, json, base64, time, glob, logging, subprocess, socket, ipaddress, tempfile, shutil
import urllib.request, urllib.parse, urllib.error
from html import unescape
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler

try:
    import requests
except ImportError:
    print("[syncRedmine] 缺少依赖: pip3 install requests")
    sys.exit(1)

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QGroupBox, QFormLayout, QMessageBox,
    QFrame, QSystemTrayIcon, QMenu, QComboBox, QPlainTextEdit,
    QGraphicsDropShadowEffect, QCheckBox, QScrollArea, QWidget,
)
from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QFileSystemWatcher,
    QPropertyAnimation, QEasingCurve, QRectF,
)
from PyQt5.QtGui import (
    QFont, QIcon, QPixmap, QPainter, QColor,
    QLinearGradient, QPainterPath,
)

# ═══════════════════════════════════════════════════════════════════════════════
# 常量
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
AUTO_UPDATE_DEFAULT_USERNAME = 'hmt'
AUTO_UPDATE_DEFAULT_PASSWORD = '123456'
AUTO_UPDATE_DEFAULT_REPO_PATH = os.path.dirname(os.path.abspath(__file__))


BENCHMARK_NET = ipaddress.ip_network('198.18.0.0/15')
PREFERRED_IFACE_PREFIXES = ('en', 'eth', 'wl', 'ww')
VIRTUAL_IFACE_PREFIXES = (
    'lo', 'docker', 'br-', 'veth', 'virbr', 'tun', 'tap', 'wg',
    'tailscale', 'zt', 'mihomo', 'clash',
)


def _get_default_ipv4_iface():
    try:
        output = subprocess.check_output(
            ['ip', '-4', 'route', 'show', 'default'],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ''

    for line in output.splitlines():
        parts = line.split()
        if 'dev' in parts:
            idx = parts.index('dev')
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return ''


def _iter_ipv4_candidates():
    try:
        output = subprocess.check_output(
            ['ip', '-4', '-o', 'addr', 'show', 'up', 'scope', 'global'],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        output = ''

    seen = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != 'inet':
            continue
        iface = parts[1]
        ip = parts[3].split('/', 1)[0]
        key = (iface, ip)
        if key not in seen:
            seen.add(key)
            yield iface, ip

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            key = ('', ip)
            if key not in seen:
                seen.add(key)
                yield '', ip
    except OSError:
        return


def _ipv4_priority(iface, ip, default_iface):
    try:
        addr = ipaddress.IPv4Address(ip)
    except ipaddress.AddressValueError:
        return -10_000

    if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return -10_000

    iface_name = (iface or '').lower()
    score = 0

    if iface and default_iface and iface == default_iface:
        score += 40
    if iface_name.startswith(PREFERRED_IFACE_PREFIXES):
        score += 80
    if iface and not iface_name.startswith(VIRTUAL_IFACE_PREFIXES):
        score += 30
    if addr.is_private and addr not in BENCHMARK_NET:
        score += 20
    if addr in BENCHMARK_NET:
        score -= 100
    if not iface:
        score -= 5

    return score


def detect_local_ipv4():
    default_iface = _get_default_ipv4_iface()
    candidates = list(_iter_ipv4_candidates())
    if not candidates:
        return ''

    _, ip = max(candidates, key=lambda item: _ipv4_priority(item[0], item[1], default_iface))
    return ip


LOCAL_MACHINE_IP = detect_local_ipv4()

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


APP_STYLE_SHEET = """
QWidget {
    font-family: "Noto Sans CJK SC", "Microsoft YaHei", "PingFang SC", sans-serif;
    color: #0f172a;
}
QDialog {
    background: #eef4fb;
}
QFrame#DialogShell {
    background: #ffffff;
    border: 1px solid #dbe4ee;
    border-radius: 24px;
}
QFrame#SectionPanel {
    background: #fbfcff;
    border: 1px solid #e7edf5;
    border-radius: 18px;
}
QFrame#InfoStrip {
    background: #f7faff;
    border: 1px solid #dbe7ff;
    border-radius: 16px;
}
QFrame#FieldReveal {
    background: transparent;
    border: none;
    border-left: 2px solid #dbe7ff;
}
QFrame#Divider {
    background: #e8eef5;
    min-height: 1px;
    max-height: 1px;
    border: none;
}
QLabel#HeroEyebrow {
    color: rgba(255, 255, 255, 0.74);
    font-size: 10pt;
    font-weight: 600;
    letter-spacing: 0.08em;
}
QLabel#HeroTitle {
    color: #ffffff;
    font-size: 18pt;
    font-weight: 700;
}
QLabel#HeroText {
    color: rgba(255, 255, 255, 0.84);
    font-size: 10pt;
    line-height: 1.45em;
}
QLabel#SectionTitle {
    color: #0f172a;
    font-size: 11pt;
    font-weight: 700;
}
QLabel#SectionDesc {
    color: #64748b;
    font-size: 9pt;
    padding-bottom: 2px;
}
QLabel#FieldCaption {
    color: #475467;
    font-size: 9.4pt;
    font-weight: 600;
}
QLabel#FieldHint {
    color: #94a3b8;
    font-size: 8.8pt;
}
QLabel#InlineState {
    color: #1d4ed8;
    font-size: 9.1pt;
    font-weight: 700;
}
QLabel#InlineSummary {
    color: #64748b;
    font-size: 9pt;
    line-height: 1.45em;
}
QLabel#ValueText {
    color: #0f172a;
    font-size: 10pt;
    line-height: 1.5em;
}
QLabel#MetaText {
    color: #475467;
    font-size: 9.3pt;
    line-height: 1.4em;
}
QLabel#StatusText {
    color: #334155;
    font-size: 10pt;
    line-height: 1.45em;
}
QLineEdit, QComboBox, QPlainTextEdit {
    background: #ffffff;
    border: 1px solid #d6deea;
    border-radius: 12px;
    padding: 8px 12px;
    font-size: 10pt;
    selection-background-color: #2563eb;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {
    border: 1px solid #3b82f6;
    background: #fdfefe;
}
QComboBox {
    padding-right: 16px;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QPushButton {
    min-height: 44px;
    padding: 0 18px;
    border-radius: 14px;
    font-size: 10.5pt;
    font-weight: 700;
    border: none;
}
QPushButton#PrimaryButton {
    background: #2563eb;
    color: #ffffff;
}
QPushButton#PrimaryButton:hover {
    background: #1d4ed8;
}
QPushButton#PrimaryButton:pressed {
    background: #1e40af;
}
QPushButton#SecondaryButton {
    background: #e5e7eb;
    color: #0f172a;
}
QPushButton#SecondaryButton:hover {
    background: #dbe1ea;
}
QPushButton#GhostButton {
    min-height: 36px;
    padding: 0 14px;
    border-radius: 12px;
    border: 1px solid #d7e5ff;
    background: #f8fbff;
    color: #1d4ed8;
    font-size: 9.6pt;
    font-weight: 600;
}
QPushButton#GhostButton:hover {
    background: #eef4ff;
    border: 1px solid #bfd3ff;
}
QPushButton#GhostButton:pressed {
    background: #e0ecff;
}
QPushButton#LinkButton {
    min-height: 30px;
    background: transparent;
    color: #2563eb;
    padding: 0 4px;
    font-weight: 600;
}
QPushButton#LinkButton:hover {
    color: #1d4ed8;
}
QPushButton:disabled {
    background: #cbd5e1;
    color: #94a3b8;
}
QPlainTextEdit#ErrorDetail {
    background: #fff5f5;
    color: #b42318;
    border: 1px solid #fecaca;
    border-radius: 14px;
    padding: 10px 12px;
    font-size: 9.5pt;
}
QMenu {
    background: #0f172a;
    color: #f8fafc;
    border: 1px solid #1e293b;
    border-radius: 16px;
    padding: 8px;
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
    logger = logging.getLogger('syncRedmine')
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

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
        logger.addHandler(handler)
        logger.info("日志系统已启动，日志文件: %s，保留天数: %s", LOG_FILE, LOG_RETENTION_DAYS)
    except Exception as e:
        logger.addHandler(logging.NullHandler())
        print(f"[syncRedmine] 初始化日志失败: {e}", file=sys.stderr)

    return logger


logger = setup_logging()


# ═══════════════════════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════════════════════
def load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        for k in ('gerrit_password', 'redmine_password', 'update_password'):
            if d.get(k):
                d[k] = base64.b64decode(d[k]).decode('utf-8')
        if 'auto_update_enabled' not in d:
            d['auto_update_enabled'] = True
        if not d.get('update_host') and LOCAL_MACHINE_IP:
            d['update_host'] = LOCAL_MACHINE_IP
        if not d.get('update_username'):
            d['update_username'] = AUTO_UPDATE_DEFAULT_USERNAME
        if not d.get('update_password'):
            d['update_password'] = AUTO_UPDATE_DEFAULT_PASSWORD
        # 迁移旧字段：update_remote_path（单文件路径）→ update_repo_path（仓库目录）
        if not d.get('update_repo_path'):
            old = d.pop('update_remote_path', '') or ''
            d['update_repo_path'] = os.path.dirname(old) if old.endswith('.py') else (old or AUTO_UPDATE_DEFAULT_REPO_PATH)
        d['update_port'] = str(d.get('update_port', '22') or '22')
        return d
    except Exception:
        return None

def save_config(cfg):
    os.makedirs(COMMIT_TOOL_DIR, exist_ok=True)
    d = dict(cfg)
    for k in ('gerrit_password', 'redmine_password', 'update_password'):
        if d.get(k):
            d[k] = base64.b64encode(d[k].encode('utf-8')).decode('utf-8')
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    logger.info("账号配置已保存: %s", CONFIG_FILE)


# ═══════════════════════════════════════════════════════════════════════════════
# commit_data.log 解析
# ═══════════════════════════════════════════════════════════════════════════════
def parse_commit_log(path=DEFAULT_LOG):
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError as e:
        logger.warning("读取 commit_data.log 失败: %s", e)
        return None

    try:
        content = raw.decode('utf-8')
    except UnicodeDecodeError:
        try:
            content = raw.decode('gb18030')
            logger.warning("commit_data.log 非 UTF-8，已回退按 gb18030 解码")
        except UnicodeDecodeError:
            content = raw.decode('utf-8', errors='replace')
            logger.warning("commit_data.log 编码异常，已使用 replace 容错解码")

    fields = {}
    for key in ['Bug number', 'Topic ID', 'Author', 'Root Cause', 'Solution',
                'Test_Report', 'Test_Suggestion', 'Comment']:
        m = re.search(rf'^{re.escape(key)}:(.+)$', content, re.MULTILINE)
        fields[key] = m.group(1).strip() if m else ''
    return fields


def extract_first_number(text):
    m = re.search(r'\d+', text or '')
    return m.group() if m else ''


def normalize_topic_id(topic):
    """与 commit_tool/config.sh 中 push topic 的处理保持一致。"""
    topic = (topic or '').strip()
    if 'revert' in topic.lower():
        topic = topic[8:].strip() if len(topic) > 8 else ''
    return topic


def get_gerrit_topics(fields):
    """优先按 Bug number 关联，同时兼容 Topic ID。"""
    topics = []
    bug_number = extract_first_number(fields.get('Bug number', ''))
    topic_id = normalize_topic_id(fields.get('Topic ID', ''))
    if bug_number:
        topics.append(bug_number)
    if topic_id and topic_id not in topics:
        topics.append(topic_id)
    return topics


def parse_gerrit_time(value):
    """解析 Gerrit updated 时间，按 UTC 处理。"""
    value = (value or '').strip()
    if not value:
        return None
    try:
        if '.' in value:
            prefix, frac = value.split('.', 1)
            frac = re.sub(r'\D', '', frac)
            value = f"{prefix}.{frac[:6].ljust(6, '0')}"
            dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S.%f')
        else:
            dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _decode_gerrit_json(text):
    """去掉 Gerrit JSON 防护前缀 )]}'"""
    if text.startswith(")]}'"):
        text = text[text.index("\n") + 1:]
    return json.loads(text.strip())


# ── Gerrit Cookie 认证 ────────────────────────────────────────────────────────
GERRIT_COOKIE_CACHE = "/tmp/.syncredmine_gerrit_cookie"

def _extract_gerrit_cookie(headers):
    cookie_values = []
    if hasattr(headers, 'get_all'):
        cookie_values.extend(headers.get_all("Set-Cookie") or [])
    single = headers.get("Set-Cookie", "")
    if single:
        cookie_values.append(single)
    cookie_blob = "\n".join(cookie_values)
    m = re.search(r"GerritAccount=([^;]+)", cookie_blob)
    return m.group(1) if m else None


def _gerrit_login(base, username, password, timeout=15):
    """表单 POST 登录获取 GerritAccount cookie（与 gerrit_api.py 同机制）"""
    # 先检查缓存
    if os.path.exists(GERRIT_COOKIE_CACHE):
        try:
            with open(GERRIT_COOKIE_CACHE) as f:
                cache = json.load(f)
            if (cache.get("base_url") == base
                    and time.time() - cache.get("time", 0) < 7200):
                return cache["cookie"]
        except Exception:
            pass

    login_url = f"{base}/login/%2F"
    data = urllib.parse.urlencode({"username": username, "password": password}).encode()

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(urllib.request.Request(login_url, data=data), timeout=timeout) as resp:
            cookie = _extract_gerrit_cookie(resp.headers)
            if cookie:
                try:
                    with open(GERRIT_COOKIE_CACHE, "w") as f:
                        json.dump({"base_url": base, "cookie": cookie, "time": time.time()}, f)
                    os.chmod(GERRIT_COOKIE_CACHE, 0o600)
                except Exception:
                    pass
                return cookie
    except urllib.error.HTTPError as e:
        cookie = _extract_gerrit_cookie(e.headers)
        if cookie:
            try:
                with open(GERRIT_COOKIE_CACHE, "w") as f:
                    json.dump({"base_url": base, "cookie": cookie, "time": time.time()}, f)
                os.chmod(GERRIT_COOKIE_CACHE, 0o600)
            except Exception:
                pass
            return cookie
    raise RuntimeError("Gerrit 登录失败：未获取到 cookie")


def _gerrit_api_get(base, cookie, path, params=None, timeout=30):
    """用 cookie 调用 Gerrit REST API"""
    url = f"{base}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url)
    req.add_header("Cookie", f"GerritAccount={cookie}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _decode_gerrit_json(resp.read().decode("utf-8"))


def fetch_gerrit_changes(config, topics, timeout=10):
    """查询一个或多个 topic 下的 changes（Cookie 认证）。"""
    base = config['gerrit_url'].rstrip('/')
    if isinstance(topics, str):
        topics = [topics]

    try:
        cookie = _gerrit_login(
            base, config['gerrit_username'], config['gerrit_password'],
            timeout=timeout)
    except Exception as e:
        logger.warning("Gerrit 登录失败: %s", e)
        return None

    result = {}
    has_success = False

    for topic in topics:
        try:
            changes = _gerrit_api_get(base, cookie, "changes/",
                                      {"q": f"topic:{topic}"},
                                      timeout=timeout)
            has_success = True
            for c in changes:
                num = c.get('_number')
                if num:
                    result[num] = {
                        'updated': c.get('updated', ''),
                        'project': c.get('project', ''),
                    }
        except Exception as e:
            logger.warning("查询 Gerrit topic=%s 失败: %s", topic, e)
            continue

    return result if has_success else None


def build_gerrit_change_url(base, change_number, change_info=None):
    project = (change_info or {}).get('project', '')
    if project:
        return f"{base}/c/{project}/+/{change_number}"
    return f"{base}/c/+/{change_number}"


def _strip_html_tags(text):
    return re.sub(r'<[^>]+>', '', text or '')


def parse_solver_options_from_issue_html(html):
    """从 Redmine issue/edit 页面中解析“解决者”下拉候选项。"""
    label_match = re.search(
        r'<label[^>]+for="(?P<select_id>issue_custom_field_values_\d+)"[^>]*>'
        r'\s*(?:<span[^>]*>)?\s*解决者\s*(?:</span>)?\s*</label>',
        html or '', re.IGNORECASE | re.DOTALL)
    if not label_match:
        return []

    select_id = label_match.group('select_id')
    select_match = re.search(
        rf'<select[^>]*id="{re.escape(select_id)}"[^>]*>(?P<options>.*?)</select>',
        html or '', re.IGNORECASE | re.DOTALL)
    if not select_match:
        return []

    options = []
    seen_ids = set()
    for option_match in re.finditer(
            r'<option(?P<attrs>[^>]*)value="(?P<value>[^"]*)"[^>]*>(?P<label>.*?)</option>',
            select_match.group('options'), re.IGNORECASE | re.DOTALL):
        user_id = unescape(option_match.group('value')).strip()
        if not user_id or user_id in seen_ids:
            continue
        label = unescape(_strip_html_tags(option_match.group('label'))).replace('\xa0', ' ').strip()
        if not label:
            label = f"用户 {user_id}"
        options.append({
            'id': user_id,
            'label': label,
            'selected': 'selected' in option_match.group('attrs').lower(),
        })
        seen_ids.add(user_id)
    return options


def fetch_redmine_solver_choices(config, issue_id, timeout=10):
    """获取当前登录用户与“解决者”候选列表。"""
    auth = (config['redmine_username'], config['redmine_password'])
    base = config['redmine_url'].rstrip('/')
    result = {
        'current_user_id': None,
        'current_user_label': '',
        'options': [],
    }

    try:
        resp = requests.get(f'{base}/my/account.json', auth=auth, timeout=timeout)
        if resp.status_code == 200:
            user = resp.json().get('user', {})
            user_id = user.get('id')
            if user_id is not None:
                result['current_user_id'] = str(user_id)
            first = (user.get('firstname') or '').strip()
            last = (user.get('lastname') or '').strip()
            login = (user.get('login') or '').strip()
            display_name = ''.join(part for part in [last, first] if part) or login
            if login and display_name and login not in display_name:
                result['current_user_label'] = f"{login}{display_name}"
            else:
                result['current_user_label'] = display_name or login or (f"用户 {user_id}" if user_id else '')
    except Exception as e:
        logger.warning("获取当前 Redmine 用户失败: %s", e)

    issue_paths = [f'/issues/{issue_id}/edit', f'/issues/{issue_id}']
    for path in issue_paths:
        try:
            resp = requests.get(f'{base}{path}', auth=auth, timeout=timeout)
            if resp.status_code != 200:
                continue
            options = parse_solver_options_from_issue_html(resp.text)
            if options:
                result['options'] = options
                break
        except Exception as e:
            logger.warning("解析 Redmine 解决者候选失败 path=%s: %s", path, e)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 解决者候选异步加载线程
# ═══════════════════════════════════════════════════════════════════════════════
class SolverChoicesLoader(QThread):
    loaded_sig = pyqtSignal(int, object)

    def __init__(self, config, issue_id, request_id, parent=None):
        super().__init__(parent)
        self.config = dict(config or {})
        self.issue_id = issue_id
        self.request_id = request_id

    def run(self):
        try:
            result = fetch_redmine_solver_choices(self.config, self.issue_id)
        except Exception as e:
            logger.warning("异步加载解决者候选失败: issue=%s err=%s", self.issue_id, e)
            result = {
                'current_user_id': None,
                'current_user_label': '',
                'options': [],
            }
        self.loaded_sig.emit(self.request_id, result)


class AutoUpdateWorker(QThread):
    finished_sig = pyqtSignal(bool, bool, str)  # ok, changed, message

    def __init__(self, config, local_script_path, parent=None):
        super().__init__(parent)
        self.config = dict(config or {})
        self.local_script_path = os.path.abspath(local_script_path)

    @staticmethod
    def _parse_port(raw):
        try:
            port = int(str(raw).strip() or '22')
            return port if 1 <= port <= 65535 else 22
        except (TypeError, ValueError):
            return 22

    # 需要从远端仓库同步的文件列表
    REPO_FILES = ['syncRedmine.py', 'install.sh', 'requirements.txt']

    def run(self):
        host      = (self.config.get('update_host') or '').strip()
        username  = (self.config.get('update_username') or '').strip()
        password  = self.config.get('update_password') or ''
        repo_path = (self.config.get('update_repo_path') or '').strip().rstrip('/')
        port      = self._parse_port(self.config.get('update_port', '22'))

        if not host or not username or not password or not repo_path:
            logger.info("自动更新跳过：配置不完整 host=%s username=%s repo_path=%s",
                        bool(host), bool(username), bool(repo_path))
            self.finished_sig.emit(True, False, "自动更新未配置完整，已跳过")
            return

        try:
            import paramiko
        except ImportError:
            logger.warning("自动更新失败：缺少 paramiko 依赖")
            self.finished_sig.emit(False, False, "缺少 paramiko 依赖，请重新运行 install.sh")
            return

        client = None
        sftp   = None
        tmp_dir = None
        try:
            logger.info("开始自动更新检查：host=%s port=%s repo=%s", host, port, repo_path)
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(hostname=host, port=port, username=username,
                           password=password, timeout=10)
            sftp = client.open_sftp()

            # ── 下载仓库文件到临时目录 ──────────────────────────────────────
            tmp_dir = tempfile.mkdtemp(prefix='syncRedmine_update_')
            downloaded = {}
            for fname in self.REPO_FILES:
                remote_file = f"{repo_path}/{fname}"
                local_file  = os.path.join(tmp_dir, fname)
                try:
                    sftp.get(remote_file, local_file)
                    with open(local_file, 'rb') as f:
                        downloaded[fname] = f.read()
                    logger.info("已下载：%s", remote_file)
                except FileNotFoundError:
                    if fname == 'syncRedmine.py':
                        raise RuntimeError(f"远端仓库中找不到 syncRedmine.py：{remote_file}")
                    logger.warning("远端可选文件不存在，跳过：%s", remote_file)

            # ── 比较主脚本，判断是否有更新 ────────────────────────────────
            try:
                with open(self.local_script_path, 'rb') as f:
                    local_bytes = f.read()
            except FileNotFoundError:
                local_bytes = b''

            if downloaded.get('syncRedmine.py') == local_bytes:
                logger.info("自动更新检查完成：当前已是最新版本")
                self.finished_sig.emit(True, False, "已检查更新，无新版本")
                return

            # ── 执行 install.sh 完成安装 ─────────────────────────────────
            install_sh = os.path.join(tmp_dir, 'install.sh')
            if not os.path.exists(install_sh):
                raise RuntimeError("远端仓库中找不到 install.sh，无法自动安装")

            os.chmod(install_sh, 0o755)
            env = {**os.environ, 'AUTO_INSTALL': '1'}
            result = subprocess.run(
                ['bash', install_sh],
                cwd=tmp_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or '').strip()
                raise RuntimeError(f"install.sh 返回错误 (code={result.returncode}):\n{detail}")

            logger.info("自动更新成功，install.sh 执行完毕")
            self.finished_sig.emit(True, True, "检测到新版本，已同步仓库并完成安装")

        except Exception as e:
            logger.exception("自动更新失败")
            self.finished_sig.emit(False, False, str(e))
        finally:
            try:
                if sftp is not None:
                    sftp.close()
            except Exception:
                pass
            try:
                if client is not None:
                    client.close()
            except Exception:
                pass
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════════
# UI 基础
# ═══════════════════════════════════════════════════════════════════════════════
def apply_shadow(widget, blur=42, y_offset=12, alpha=30):
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(blur)
    shadow.setOffset(0, y_offset)
    shadow.setColor(QColor(15, 23, 42, alpha))
    widget.setGraphicsEffect(shadow)


def make_badge(text, bg='#dbeafe', fg='#1d4ed8'):
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    label.setStyleSheet(
        "QLabel{"
        f"background:{bg};color:{fg};"
        "border-radius:11px;padding:6px 10px;"
        "font-size:9pt;font-weight:700;}")
    return label


def tint_badge(label, text, bg, fg):
    label.setText(text)
    label.setStyleSheet(
        "QLabel{"
        f"background:{bg};color:{fg};"
        "border-radius:11px;padding:6px 10px;"
        "font-size:9pt;font-weight:700;}")


def make_divider():
    line = QFrame()
    line.setObjectName("Divider")
    return line


class GradientPanel(QFrame):
    def __init__(self, start_color, end_color, glow_color, parent=None):
        super().__init__(parent)
        self.start_color = QColor(start_color)
        self.end_color = QColor(end_color)
        self.glow_color = QColor(glow_color)
        self.setMinimumHeight(156)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        path = QPainterPath()
        path.addRoundedRect(rect, 24, 24)

        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0, self.start_color)
        gradient.setColorAt(1, self.end_color)
        painter.fillPath(path, gradient)
        painter.setClipPath(path)

        glow = QColor(self.glow_color)
        glow.setAlpha(85)
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QRectF(rect.width() * 0.64, -rect.height() * 0.18,
                                   rect.width() * 0.42, rect.height() * 0.80))

        soft = QColor(255, 255, 255, 26)
        painter.setBrush(soft)
        painter.drawEllipse(QRectF(-rect.width() * 0.10, rect.height() * 0.62,
                                   rect.width() * 0.36, rect.height() * 0.48))

        painter.setPen(QColor(255, 255, 255, 34))
        painter.drawPath(path)
        super().paintEvent(event)


class AnimatedDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._fade_anim = None
        self._has_animated = False

    def showEvent(self, event):
        super().showEvent(event)
        if self._has_animated:
            return
        self._has_animated = True
        self.setWindowOpacity(0.0)
        self._fade_anim = QPropertyAnimation(self, b'windowOpacity', self)
        self._fade_anim.setDuration(220)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_anim.start()


# ═══════════════════════════════════════════════════════════════════════════════
# 图标
# ═══════════════════════════════════════════════════════════════════════════════
def make_icon(color='#4a90d9', badge=None):
    pix = QPixmap(36, 36)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    base = QColor(color)
    rect = QRectF(2, 2, 32, 32)
    path = QPainterPath()
    path.addRoundedRect(rect, 10, 10)
    grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
    grad.setColorAt(0, base.lighter(118))
    grad.setColorAt(1, base.darker(110))
    p.fillPath(path, grad)
    p.setPen(QColor(255, 255, 255, 42))
    p.drawPath(path)
    p.setPen(Qt.white)
    p.setFont(QFont('', 13, QFont.Bold))
    p.drawText(pix.rect(), Qt.AlignCenter, 'R')
    if badge:
        p.setBrush(QColor('#f44336'))
        p.setPen(Qt.NoPen)
        p.drawEllipse(23, 1, 11, 11)
        p.setPen(Qt.white)
        p.setFont(QFont('', 7, QFont.Bold))
        p.drawText(QtCore.QRect(23, 1, 11, 11), Qt.AlignCenter, badge[:1])
    p.end()
    return QIcon(pix)


# ═══════════════════════════════════════════════════════════════════════════════
# Gerrit 轮询线程 —— 检测 push 完成
# ═══════════════════════════════════════════════════════════════════════════════
class GerritPoller(QThread):
    push_detected = pyqtSignal(str)   # gerrit change url
    status_msg    = pyqtSignal(str)   # 状态提示
    timed_out     = pyqtSignal()

    def __init__(self, config, topics, trigger_time=None, initial_changes=None):
        super().__init__()
        self.config          = config
        self.topics          = topics
        self.trigger_time    = trigger_time
        self.initial_changes = initial_changes
        self._stop           = False
        self._base           = config['gerrit_url'].rstrip('/')

    def cancel(self):
        self._stop = True
        logger.info("已取消 Gerrit 轮询: topics=%s", self.topics)

    # ── Gerrit REST API ──────────────────────────────────────────────────────
    def _get_changes(self):
        return fetch_gerrit_changes(self.config, self.topics)

    def _build_url(self, change_number, change_info=None):
        return build_gerrit_change_url(self._base, change_number, change_info)

    def _find_recent_change(self, changes):
        if not self.trigger_time:
            return None
        threshold = self.trigger_time - timedelta(seconds=RECENT_PUSH_SLACK)
        candidates = []
        for num, info in changes.items():
            updated_at = parse_gerrit_time(info.get('updated'))
            if updated_at and updated_at >= threshold:
                candidates.append((updated_at, num, info))
        if not candidates:
            return None
        _, num, info = max(candidates, key=lambda item: item[0])
        return num, info

    @staticmethod
    def _detect_change(initial, current):
        candidates = []
        for num, info in current.items():
            prev = initial.get(num)
            if prev is None or info.get('updated', '') != prev.get('updated', ''):
                updated_at = parse_gerrit_time(info.get('updated')) or datetime.min.replace(
                    tzinfo=timezone.utc)
                candidates.append((updated_at, num, info))
        if not candidates:
            return None
        _, num, info = max(candidates, key=lambda item: item[0])
        return num, info

    # ── 主轮询逻辑 ────────────────────────────────────────────────────────────
    def run(self):
        logger.info("开始 Gerrit 轮询: topics=%s", self.topics)
        self.status_msg.emit("等待 push 完成...")
        initial = self.initial_changes

        # 若建立基线前 push 已完成，则直接识别最近更新的 change
        if initial is not None:
            recent = self._find_recent_change(initial)
            if recent:
                num, info = recent
                logger.info("命中最近更新 change: change=%s topics=%s", num, self.topics)
                self.push_detected.emit(self._build_url(num, info))
                return

        elapsed = 0
        while not self._stop and elapsed < POLL_TIMEOUT:
            current = self._get_changes()
            if current is None:
                status = "建立 Gerrit 基线失败" if initial is None else "Gerrit 连接失败"
                self.status_msg.emit(f"{status}，重试中 ({elapsed}s)...")
            elif initial is None:
                # 首次成功拿到快照时，先判断 push 是否已经发生，避免竞态导致漏检
                recent = self._find_recent_change(current)
                if recent:
                    num, info = recent
                    logger.info("建立基线前已检测到 push: change=%s topics=%s", num, self.topics)
                    self.push_detected.emit(self._build_url(num, info))
                    return
                initial = current
                logger.info("已建立 Gerrit 基线: topics=%s changes=%s", self.topics, len(current))
                self.status_msg.emit("已建立 Gerrit 基线，等待 push 完成...")
            else:
                detected = self._detect_change(initial, current)
                if detected:
                    num, info = detected
                    logger.info("检测到 Gerrit push 完成: change=%s topics=%s", num, self.topics)
                    self.push_detected.emit(self._build_url(num, info))
                    return
                self.status_msg.emit(f"等待 push 完成... ({elapsed}s)")

            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

        if not self._stop:
            logger.warning("等待 Gerrit push 超时: topics=%s timeout=%ss", self.topics, POLL_TIMEOUT)
            self.timed_out.emit()


# ═══════════════════════════════════════════════════════════════════════════════
# Redmine 同步线程
# ═══════════════════════════════════════════════════════════════════════════════
class SyncWorker(QThread):
    log_sig      = pyqtSignal(str)
    finished_sig = pyqtSignal(bool, str)

    def __init__(self, config, fields, gerrit_url, hours=0.5, status_name='OnGoing',
                 solver_user_id=None):
        super().__init__()
        self.config      = config
        self.fields      = fields
        self.gerrit_url  = gerrit_url
        self.hours       = hours
        self.status_name = status_name
        self.solver_user_id = str(solver_user_id) if solver_user_id else None
        self.auth       = (config['redmine_username'], config['redmine_password'])
        self.base       = config['redmine_url'].rstrip('/')

    def _get(self, path, **kw):
        return requests.get(f'{self.base}{path}', auth=self.auth, timeout=10, **kw)

    def _put(self, path, payload):
        return requests.put(
            f'{self.base}{path}', auth=self.auth,
            headers={'Content-Type': 'application/json'},
            data=json.dumps(payload), timeout=10)

    def run(self):
        try:
            issue_number = extract_first_number(self.fields.get('Bug number', ''))
            if not issue_number:
                logger.warning("同步失败: Bug number 无效, fields=%s", self.fields.get('Bug number', ''))
                self.finished_sig.emit(False, f"Bug number 无效")
                return
            issue_id = int(issue_number)
            logger.info("开始同步 Redmine: issue=%s status=%s hours=%s", issue_id,
                        self.status_name, self.hours)

            self.log_sig.emit(f"获取 issue #{issue_id}...")
            r = self._get(f'/issues/{issue_id}.json')
            if r.status_code != 200:
                logger.warning("获取 issue 失败: issue=%s http=%s", issue_id, r.status_code)
                self.finished_sig.emit(False, f"获取 issue 失败: HTTP {r.status_code}")
                return

            cf_map = {cf['name']: cf['id']
                      for cf in r.json()['issue'].get('custom_fields', [])}

            self.log_sig.emit("获取用户信息...")
            r2   = self._get('/my/account.json')
            uid  = r2.json()['user']['id'] if r2.status_code == 200 else None
            default_solver_uid = str(uid) if uid is not None else None
            solver_uid = self.solver_user_id or default_solver_uid

            self.log_sig.emit("获取状态列表...")
            r3 = self._get('/issue_statuses.json')
            target_status_id = None
            if r3.status_code == 200:
                for s in r3.json().get('issue_statuses', []):
                    if s['name'].lower() == self.status_name.lower():
                        target_status_id = s['id']
                        break
            if not target_status_id:
                logger.warning("未匹配到目标状态: issue=%s status=%s", issue_id, self.status_name)

            # 组装自定义字段
            cfs = []
            if FIX_FIELD_NAME in cf_map:
                cfs.append({'id': cf_map[FIX_FIELD_NAME], 'value': self.gerrit_url or PLACEHOLDER})
            for log_key, rf_name in FIELD_MAP.items():
                val = self.fields.get(log_key, '').strip() or PLACEHOLDER
                if rf_name in cf_map:
                    cfs.append({'id': cf_map[rf_name], 'value': val})
            # 解决者 —— 优先自定义字段，其次 assigned_to_id
            solver_field_id = cf_map.get(SOLVER_FIELD_NAME)
            if solver_uid and solver_field_id:
                cfs.append({'id': solver_field_id, 'value': solver_uid})
                source = "manual" if self.solver_user_id else "default"
                self.log_sig.emit("solver -> custom_field (uid=%s source=%s)" % (solver_uid, source))
            elif solver_uid:
                source = "manual" if self.solver_user_id else "default"
                self.log_sig.emit("solver -> assigned_to_id (uid=%s source=%s)" % (solver_uid, source))

            # 工时 —— 优先查找名称含"工时"的自定义字段
            hours_field_id = None
            for name, fid in cf_map.items():
                if '工时' in name:
                    hours_field_id = fid
                    break
            if hours_field_id:
                cfs.append({'id': hours_field_id, 'value': str(self.hours)})

            update = {'done_ratio': 100}
            if target_status_id:
                update['status_id'] = target_status_id
            # 若解决者不在自定义字段，走 assigned_to_id
            if solver_uid and not solver_field_id:
                update['assigned_to_id'] = int(solver_uid) if str(solver_uid).isdigit() else solver_uid
            if cfs:
                update['custom_fields'] = cfs

            self.log_sig.emit(f"提交更新 issue #{issue_id}...")
            r4 = self._put(f'/issues/{issue_id}.json', {'issue': update})
            if r4.status_code in (200, 204):
                msg = f"issue #{issue_id} 同步成功！"
            else:
                logger.warning("更新 issue 失败: issue=%s http=%s", issue_id, r4.status_code)
                self.finished_sig.emit(False,
                    f"更新失败: HTTP {r4.status_code}\n{r4.text[:300]}")
                return

            # 工时 —— 若无自定义字段，则创建 time_entry
            if not hours_field_id and self.hours > 0:
                self.log_sig.emit(f"记录工时 {self.hours} 小时...")
                te = {'time_entry': {'issue_id': issue_id, 'hours': self.hours}}
                r5 = requests.post(f'{self.base}/time_entries.json',
                                   auth=self.auth,
                                   headers={'Content-Type': 'application/json'},
                                   data=json.dumps(te), timeout=10)
                if r5.status_code in (200, 201):
                    logger.info("工时记录成功: issue=%s hours=%s", issue_id, self.hours)
                    msg += f"  工时 {self.hours}h 已记录"
                else:
                    logger.warning("工时记录失败: issue=%s http=%s", issue_id, r5.status_code)
                    msg += f"  (工时记录失败: HTTP {r5.status_code})"

            logger.info("Redmine 同步成功: issue=%s", issue_id)
            self.finished_sig.emit(True, msg)
        except Exception as e:
            logger.exception("同步 Redmine 时发生异常")
            self.finished_sig.emit(False, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# 设置对话框
# ═══════════════════════════════════════════════════════════════════════════════
class SetupDialog(AnimatedDialog):

    def __init__(self, existing=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("syncRedmine — 设置")
        self.setWindowIcon(make_icon())
        self.setFixedWidth(700)
        self.config = {}
        self._update_widgets = []
        self._update_details_expanded = False
        self._ssh_anim = None
        self._build(existing or {})

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._fix_initial_size)

    def _fix_initial_size(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            self.setMaximumHeight(screen.availableGeometry().height() - 80)
        self.adjustSize()
        self.setFixedHeight(self.height())

    def _le(self, placeholder='', pw=False, val=''):
        e = QLineEdit(val)
        e.setPlaceholderText(placeholder)
        e.setFixedHeight(44)
        if not pw:
            e.setClearButtonEnabled(True)
        if pw:
            e.setEchoMode(QLineEdit.Password)
        return e

    @staticmethod
    def _panel(title, subtitle, badge_text):
        panel = QFrame()
        panel.setObjectName("SectionPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        head = QHBoxLayout()
        text_col = QVBoxLayout()
        text_col.setSpacing(4)

        ttl = QLabel(title)
        ttl.setObjectName("SectionTitle")
        desc = QLabel(subtitle)
        desc.setObjectName("SectionDesc")
        desc.setWordWrap(True)
        desc.setContentsMargins(0, 0, 0, 2)
        desc.setMinimumHeight(desc.fontMetrics().lineSpacing() + 4)

        text_col.addWidget(ttl)
        text_col.addWidget(desc)
        head.addLayout(text_col, 1)
        head.addWidget(make_badge(badge_text))
        layout.addLayout(head)
        layout.addWidget(make_divider())
        return panel, layout

    @staticmethod
    def _add_field(layout, title, widget, hint=None):
        cap = QLabel(title)
        cap.setObjectName("FieldCaption")
        layout.addWidget(cap)
        layout.addWidget(widget)
        if hint:
            lbl = QLabel(hint)
            lbl.setObjectName("FieldHint")
            lbl.setWordWrap(True)
            layout.addWidget(lbl)

    @staticmethod
    def _field_block(title, widget, hint=None):
        wrapper = QFrame()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        cap = QLabel(title)
        cap.setObjectName("FieldCaption")
        layout.addWidget(cap)
        layout.addWidget(widget)
        if hint:
            lbl = QLabel(hint)
            lbl.setObjectName("FieldHint")
            lbl.setWordWrap(True)
            layout.addWidget(lbl)
        return wrapper

    @staticmethod
    def _mkbtn(text, role, slot):
        b = QPushButton(text)
        b.setObjectName(role)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def _toggle_auto_update_fields(self, checked):
        for widget in self._update_widgets:
            widget.setEnabled(checked)
        if hasattr(self, 'update_detail_toggle'):
            self.update_detail_toggle.setEnabled(checked)
        if hasattr(self, 'update_meta'):
            self.update_meta.setVisible(checked)
        if not checked:
            self._set_update_details_visible(False)  # 带动画收起
        self._refresh_update_summary()

    def _set_update_details_visible(self, visible):
        expanded = bool(visible and self.update_enabled.isChecked())
        self._update_details_expanded = expanded
        if hasattr(self, 'update_detail_toggle'):
            self.update_detail_toggle.setText("收起 SSH 配置" if expanded else "展开 SSH 配置")
        if not hasattr(self, 'update_detail_panel'):
            return

        if self._ssh_anim is not None:
            self._ssh_anim.stop()

        target_h = self.update_detail_panel.sizeHint().height() if expanded else 0
        if expanded:
            self.update_detail_panel.show()

        anim = QPropertyAnimation(self.update_detail_panel, b"maximumHeight", self)
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(self.update_detail_panel.maximumHeight())
        anim.setEndValue(target_h)
        if not expanded:
            anim.finished.connect(self.update_detail_panel.hide)
        self._ssh_anim = anim
        anim.start()

    def _toggle_update_details(self):
        self._set_update_details_visible(not self._update_details_expanded)

    def _refresh_update_summary(self):
        if not hasattr(self, 'update_summary'):
            return
        enabled = self.update_enabled.isChecked()
        self.update_state.setText("自动更新已启用" if enabled else "自动更新已关闭")
        if not enabled:
            self.update_summary.setText("关闭后不会在每天 10:00 自动检查新脚本。")
            return

        configured = sum(bool(value) for value in (
            self.u_host.text().strip(),
            self.u_user.text().strip(),
            self.u_pass.text().strip(),
            self.u_path.text().strip(),
        ))
        state = "已配置完整" if configured == 4 else f"已配置 {configured}/4 项"
        self.update_summary.setText(f"每天 10:00 检查更新 · {state} · SSH 连接项默认折叠")

    def _build(self, cfg):
        root = QVBoxLayout(self)
        root.setSpacing(0)
        root.setContentsMargins(18, 18, 18, 18)

        shell = QFrame()
        shell.setObjectName("DialogShell")
        apply_shadow(shell, blur=52, y_offset=16, alpha=34)
        root.addWidget(shell)

        body = QVBoxLayout(shell)
        body.setSpacing(18)
        body.setContentsMargins(24, 24, 24, 24)

        # ── 滚动区（hero + Gerrit + Redmine + 自动更新）────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }")

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setSpacing(18)
        inner_layout.setContentsMargins(0, 0, 0, 0)

        hero = GradientPanel('#0f172a', '#2563eb', '#60a5fa')
        hero_layout = QVBoxLayout(hero)
        hero_layout.setSpacing(10)
        hero_layout.setContentsMargins(24, 22, 24, 22)

        hero_top = QHBoxLayout()
        hero_top.addWidget(make_badge("本地同步助手", 'rgba(255,255,255,0.16)', '#ffffff'))
        hero_top.addStretch()
        hero_top.addWidget(make_badge("设置", 'rgba(255,255,255,0.10)', '#dbeafe'))
        hero_layout.addLayout(hero_top)

        eyebrow = QLabel("syncRedmine")
        eyebrow.setObjectName("HeroEyebrow")
        hero_layout.addWidget(eyebrow)

        title = QLabel("设置 Gerrit、Redmine 与自动更新")
        title.setObjectName("HeroTitle")
        title.setWordWrap(True)
        hero_layout.addWidget(title)

        hint = QLabel("用于检测 Gerrit push、同步提交说明到 Redmine，并可按计划自动拉取新脚本。")
        hint.setObjectName("HeroText")
        hint.setWordWrap(True)
        hero_layout.addWidget(hint)

        meta = QLabel("设置仅保存在本机 ~/.commit_tool/sync_config.json")
        meta.setObjectName("HeroText")
        meta.setWordWrap(True)
        hero_layout.addWidget(meta)

        inner_layout.addWidget(hero)

        panels = QHBoxLayout()
        panels.setSpacing(16)

        g_panel, g_layout = self._panel("Gerrit", "轮询 change 状态并生成修复链接。", "检测")
        self.g_url  = self._le('http://...', val=cfg.get('gerrit_url', 'http://122.227.250.174:8085'))
        self.g_user = self._le('登录用户名', val=cfg.get('gerrit_username', ''))
        self.g_pass = self._le('登录密码', pw=True, val=cfg.get('gerrit_password', ''))
        self._add_field(g_layout, "服务器地址", self.g_url, "示例：http://host:port")
        self._add_field(g_layout, "用户名", self.g_user)
        self._add_field(g_layout, "密码", self.g_pass)
        panels.addWidget(g_panel, 1)

        r_panel, r_layout = self._panel("Redmine", "写回问题字段、工时与解决者。", "同步")
        self.r_url  = self._le('http://...', val=cfg.get('redmine_url', 'http://122.227.250.174:8078'))
        self.r_user = self._le('登录用户名', val=cfg.get('redmine_username', ''))
        self.r_pass = self._le('登录密码', pw=True, val=cfg.get('redmine_password', ''))
        self._add_field(r_layout, "服务器地址", self.r_url, "示例：http://host:port")
        self._add_field(r_layout, "用户名", self.r_user)
        self._add_field(r_layout, "密码", self.r_pass)
        panels.addWidget(r_panel, 1)

        inner_layout.addLayout(panels)

        u_panel, u_layout = self._panel(
            "自动更新",
            "每天 10:00 通过内网 SSH 拉取最新脚本；连接项默认折叠，按需展开编辑即可。",
            "更新")

        self.update_enabled = QCheckBox("启用每天 10:00 自动检查更新")
        self.update_enabled.setChecked(bool(cfg.get('auto_update_enabled', True)))
        u_layout.addWidget(self.update_enabled)

        default_update_host = cfg.get('update_host', LOCAL_MACHINE_IP)
        host_placeholder = LOCAL_MACHINE_IP or '192.168.x.x'
        host_help = f"默认当前机器：{LOCAL_MACHINE_IP}" if LOCAL_MACHINE_IP else "填写提供更新的内网机器地址"
        self.u_host = self._le(host_placeholder, val=default_update_host)
        self.u_port = self._le('22', val=str(cfg.get('update_port', '22') or '22'))
        self.u_user = self._le('SSH 用户名', val=cfg.get('update_username', AUTO_UPDATE_DEFAULT_USERNAME))
        self.u_pass = self._le('SSH 密码', pw=True, val=cfg.get('update_password', AUTO_UPDATE_DEFAULT_PASSWORD))
        self.u_path = self._le(
            AUTO_UPDATE_DEFAULT_REPO_PATH,
            val=cfg.get('update_repo_path', AUTO_UPDATE_DEFAULT_REPO_PATH))
        self._update_widgets = [self.u_host, self.u_port, self.u_user, self.u_pass, self.u_path]

        self.update_meta = QFrame()
        update_meta_layout = QHBoxLayout(self.update_meta)
        update_meta_layout.setContentsMargins(0, 0, 0, 0)
        update_meta_layout.setSpacing(12)

        update_meta_text = QVBoxLayout()
        update_meta_text.setSpacing(2)
        self.update_state = QLabel()
        self.update_state.setObjectName("InlineState")
        self.update_summary = QLabel()
        self.update_summary.setObjectName("InlineSummary")
        self.update_summary.setWordWrap(True)
        update_meta_text.addWidget(self.update_state)
        update_meta_text.addWidget(self.update_summary)
        update_meta_layout.addLayout(update_meta_text, 1)

        self.update_detail_toggle = self._mkbtn("展开 SSH 配置", "GhostButton", self._toggle_update_details)
        update_meta_layout.addWidget(self.update_detail_toggle, 0, Qt.AlignTop)
        u_layout.addWidget(self.update_meta)

        self.update_detail_panel = QFrame()
        self.update_detail_panel.setObjectName("FieldReveal")
        self.update_detail_panel.setMaximumHeight(0)   # 初始折叠
        update_detail_layout = QVBoxLayout(self.update_detail_panel)
        update_detail_layout.setContentsMargins(18, 4, 0, 0)
        update_detail_layout.setSpacing(14)

        row_a = QHBoxLayout()
        row_a.setSpacing(12)
        row_a.addWidget(self._field_block("源机器 IP / 主机名", self.u_host, host_help), 2)
        row_a.addWidget(self._field_block("SSH 端口", self.u_port, "默认 22"), 1)
        update_detail_layout.addLayout(row_a)

        row_b = QHBoxLayout()
        row_b.setSpacing(12)
        row_b.addWidget(self._field_block("SSH 用户名", self.u_user), 1)
        row_b.addWidget(self._field_block("SSH 密码", self.u_pass), 1)
        update_detail_layout.addLayout(row_b)

        update_detail_layout.addWidget(self._field_block(
            "远端仓库目录",
            self.u_path,
            f"源机器上仓库的绝对路径，默认：{AUTO_UPDATE_DEFAULT_REPO_PATH}；会下载其中的 syncRedmine.py / install.sh / requirements.txt 并执行安装。"))
        u_layout.addWidget(self.update_detail_panel)

        for widget in self._update_widgets:
            widget.textChanged.connect(self._refresh_update_summary)
        self.update_enabled.toggled.connect(self._toggle_auto_update_fields)
        # 初始化控件状态（无动画）
        checked = self.update_enabled.isChecked()
        for w in self._update_widgets:
            w.setEnabled(checked)
        self.update_detail_toggle.setEnabled(checked)
        self.update_meta.setVisible(checked)
        self._refresh_update_summary()

        inner_layout.addWidget(u_panel)
        inner_layout.addStretch()

        scroll.setWidget(inner)
        body.addWidget(scroll, 1)

        # ── 固定在底部：说明 + 按钮（不随滚动消失）─────────────────────────
        info = QFrame()
        info.setObjectName("InfoStrip")
        info_layout = QHBoxLayout(info)
        info_layout.setContentsMargins(16, 14, 16, 14)
        info_layout.setSpacing(12)
        info_layout.addWidget(make_badge("说明", '#dbeafe', '#1d4ed8'))

        note = QLabel("账号与自动更新信息仅保存在本机；自动更新只会替换当前脚本并重启，不会改动 commit_tool 现有流程。")
        note.setObjectName("MetaText")
        note.setWordWrap(True)
        info_layout.addWidget(note, 1)
        body.addWidget(info)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch()
        row.addWidget(self._mkbtn("取消", "SecondaryButton", self.reject))
        row.addWidget(self._mkbtn("保存设置", "PrimaryButton", self._save))
        body.addLayout(row)

    def _save(self):
        if not self.g_user.text().strip():
            QMessageBox.warning(self, "提示", "请输入 Gerrit 用户名"); return
        if not self.g_pass.text().strip():
            QMessageBox.warning(self, "提示", "请输入 Gerrit 密码"); return
        if not self.r_user.text().strip():
            QMessageBox.warning(self, "提示", "请输入 Redmine 用户名"); return
        if not self.r_pass.text().strip():
            QMessageBox.warning(self, "提示", "请输入 Redmine 密码"); return
        update_port = self.u_port.text().strip() or '22'
        if update_port and not update_port.isdigit():
            QMessageBox.warning(self, "提示", "SSH 端口必须是数字"); return
        self.config = {
            'gerrit_url':      self.g_url.text().strip(),
            'gerrit_username': self.g_user.text().strip(),
            'gerrit_password': self.g_pass.text().strip(),
            'redmine_url':     self.r_url.text().strip(),
            'redmine_username':self.r_user.text().strip(),
            'redmine_password':self.r_pass.text().strip(),
            'auto_update_enabled': self.update_enabled.isChecked(),
            'update_host': self.u_host.text().strip(),
            'update_port': update_port,
            'update_username': self.u_user.text().strip(),
            'update_password': self.u_pass.text().strip(),
            'update_repo_path': self.u_path.text().strip(),
        }
        save_config(self.config)
        logger.info("用户在设置窗口保存了配置")
        QMessageBox.information(self, "成功", "设置已保存 ✓")
        self.accept()


# ═══════════════════════════════════════════════════════════════════════════════
# 同步确认对话框
# ═══════════════════════════════════════════════════════════════════════════════
class SyncDialog(AnimatedDialog):
    STATUS_STYLES = {
        'idle': {
            'badge': ('待同步', '#dbeafe', '#1d4ed8'),
            'text_color': '#1d4ed8',
        },
        'running': {
            'badge': ('同步中', '#dbeafe', '#1d4ed8'),
            'text_color': '#1d4ed8',
        },
        'success': {
            'badge': ('已完成', '#dcfce7', '#15803d'),
            'text_color': '#16a34a',
        },
        'error': {
            'badge': ('失败', '#fee2e2', '#b91c1c'),
            'text_color': '#dc2626',
        },
    }

    def __init__(self, config, fields, gerrit_url, parent=None):
        super().__init__(parent)
        self.config     = config
        self.fields     = fields
        self.gerrit_url = gerrit_url
        self.worker     = None
        self._error_anim = None
        self.config_changed = False
        self.solver_choices = []
        self.default_solver_id = None
        self._solver_loader = None
        self._solver_request_id = 0
        self._solver_load_started = False
        self.issue_number = extract_first_number(self.fields.get('Bug number', '')) or (
            self.fields.get('Bug number', '').strip() or '-')
        self.setWindowTitle("syncRedmine — 同步提交信息到 Redmine")
        self.setWindowIcon(make_icon('#22c55e', '!'))
        self.setFixedWidth(920)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self._build()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._solver_load_started:
            self._solver_load_started = True
            QTimer.singleShot(0, self._load_solver_choices_async)

    @staticmethod
    def _panel(title, subtitle, badge=None, badge_style=None):
        panel = QFrame()
        panel.setObjectName("SectionPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        head = QHBoxLayout()
        text_col = QVBoxLayout()
        text_col.setSpacing(4)

        ttl = QLabel(title)
        ttl.setObjectName("SectionTitle")
        desc = QLabel(subtitle)
        desc.setObjectName("SectionDesc")
        desc.setWordWrap(True)
        desc.setContentsMargins(0, 0, 0, 2)
        desc.setMinimumHeight(desc.fontMetrics().lineSpacing() + 4)
        text_col.addWidget(ttl)
        text_col.addWidget(desc)

        head.addLayout(text_col, 1)
        if badge:
            if badge_style:
                head.addWidget(make_badge(badge, *badge_style))
            else:
                head.addWidget(make_badge(badge))
        layout.addLayout(head)
        layout.addWidget(make_divider())
        return panel, layout

    @staticmethod
    def _field_block(label_text, widget, hint=None):
        wrapper = QFrame()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        label = QLabel(label_text)
        label.setObjectName("FieldCaption")
        layout.addWidget(label)
        layout.addWidget(widget)

        if hint:
            note = QLabel(hint)
            note.setObjectName("FieldHint")
            note.setWordWrap(True)
            layout.addWidget(note)
        return wrapper

    @staticmethod
    def _make_value_widget(title, value, link=None):
        wrapper = QFrame()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("FieldCaption")
        layout.addWidget(title_lbl)

        if link:
            text = value or PLACEHOLDER
            value_lbl = QLabel(f'<a href="{link}" style="color:#2563eb;">{text}</a>')
            value_lbl.setOpenExternalLinks(True)
        else:
            value_lbl = QLabel(value or PLACEHOLDER)
        value_lbl.setObjectName("ValueText")
        value_lbl.setWordWrap(True)
        value_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        layout.addWidget(value_lbl)
        return wrapper

    @staticmethod
    def _mkbtn(text, role, slot):
        b = QPushButton(text)
        b.setObjectName(role)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(0)
        root.setContentsMargins(18, 18, 18, 18)

        shell = QFrame()
        shell.setObjectName("DialogShell")
        apply_shadow(shell, blur=52, y_offset=16, alpha=34)
        root.addWidget(shell)

        body = QVBoxLayout(shell)
        body.setSpacing(18)
        body.setContentsMargins(24, 24, 24, 24)

        hero = GradientPanel('#08111f', '#0f766e', '#34d399')
        hero_layout = QVBoxLayout(hero)
        hero_layout.setSpacing(10)
        hero_layout.setContentsMargins(24, 22, 24, 22)

        hero_top = QHBoxLayout()
        hero_top.addWidget(make_badge("检测到 Gerrit Push", 'rgba(255,255,255,0.16)', '#ffffff'))
        hero_top.addStretch()
        hero_top.addWidget(make_badge(f"Issue #{self.issue_number}", 'rgba(255,255,255,0.12)', '#d1fae5'))
        hero_layout.addLayout(hero_top)

        eyebrow = QLabel("syncRedmine")
        eyebrow.setObjectName("HeroEyebrow")
        hero_layout.addWidget(eyebrow)

        title = QLabel("准备同步到 Redmine")
        title.setObjectName("HeroTitle")
        title.setWordWrap(True)
        hero_layout.addWidget(title)

        subtitle = QLabel("提交信息已就绪，请确认工时、状态与查找思路后再执行同步。")
        subtitle.setObjectName("HeroText")
        subtitle.setWordWrap(True)
        hero_layout.addWidget(subtitle)
        body.addWidget(hero)

        top = QHBoxLayout()
        top.setSpacing(16)

        preview_panel, preview_layout = self._panel(
            "提交概览",
            "来自 commit_data.log 与 Gerrit 检测结果。",
            badge="预览")
        preview_layout.addWidget(self._make_value_widget("Issue", f"#{self.issue_number}"))
        preview_layout.addWidget(make_divider())
        preview_layout.addWidget(self._make_value_widget("提交者", self.fields.get('Author', '-') or '-'))
        preview_layout.addWidget(make_divider())
        preview_layout.addWidget(self._make_value_widget("问题根源", self.fields.get('Root Cause') or PLACEHOLDER))
        preview_layout.addWidget(make_divider())
        preview_layout.addWidget(self._make_value_widget("修复方案", self.fields.get('Solution') or PLACEHOLDER))
        preview_layout.addWidget(make_divider())
        gerrit_display = self.gerrit_url or f'未获取到，后续将填写“{PLACEHOLDER}”'
        preview_layout.addWidget(self._make_value_widget("Gerrit", gerrit_display, link=self.gerrit_url or None))
        top.addWidget(preview_panel, 1)

        edit_panel, edit_layout = self._panel(
            "同步前确认",
            "补充将写回 Redmine 的字段。",
            badge="编辑")

        self.edit_comment = QPlainTextEdit()
        self.edit_comment.setPlainText(self.fields.get('Comment', '').strip())
        self.edit_comment.setPlaceholderText('请填写查找问题的思路（留空默认“请填写”）')
        self.edit_comment.setFixedHeight(124)
        edit_layout.addWidget(self._field_block(
            "【查找问题的思路】", self.edit_comment, "支持手动补充；留空时将使用默认占位内容。"))

        meta_row = QHBoxLayout()
        meta_row.setSpacing(12)

        self.edit_hours = QLineEdit("0.5")
        self.edit_hours.setPlaceholderText('0.5')
        self.edit_hours.setFixedHeight(44)
        meta_row.addWidget(self._field_block("工时（小时）*", self.edit_hours, "默认值 0.5"), 1)

        self.combo_status = QComboBox()
        self.combo_status.addItems(["OnGoing", "Fixed"])
        self.combo_status.setCurrentIndex(0)
        self.combo_status.setFixedHeight(44)
        meta_row.addWidget(self._field_block("状态", self.combo_status, "同步时写入 Redmine 状态"), 1)
        edit_layout.addLayout(meta_row)

        self.combo_solver = QComboBox()
        self.combo_solver.setFixedHeight(44)
        self.combo_solver.setEnabled(False)
        self.combo_solver.addItem("正在加载解决者...", None)
        edit_layout.addWidget(self._field_block(
            "解决者", self.combo_solver, "默认使用当前登录 Redmine 用户，也可手动改选。"))
        edit_layout.addStretch()
        top.addWidget(edit_panel, 1)

        body.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(16)

        updates_panel, updates_layout = self._panel(
            "即将更新的字段",
            "同步后会覆盖以下 Redmine 内容。",
            badge="写入")
        tags = ["状态", "完成度→100%", "解决者", "修复情况",
                "问题根源", "修复方案", "自测情况", "建议", "查找问题的思路"]
        tag_html = " ".join(
            f'<span style="background:#e3f2fd;color:#1565c0;border-radius:9px;'
            f'padding:4px 10px;font-size:9pt;">{t}</span>' for t in tags)
        note = QLabel(tag_html)
        note.setWordWrap(True)
        note.setTextFormat(Qt.RichText)
        updates_layout.addWidget(note)

        updates_info = QLabel("工时会优先写入名称包含“工时”的自定义字段，无匹配时回退到 time_entries。")
        updates_info.setObjectName("MetaText")
        updates_info.setWordWrap(True)
        updates_layout.addWidget(updates_info)
        bottom.addWidget(updates_panel, 1)

        feedback_panel, feedback_layout = self._panel(
            "同步反馈",
            "点击“立即同步”后在这里查看进度与异常详情。",
            badge="待同步",
            badge_style=('#dbeafe', '#1d4ed8'))
        self.status_pill = feedback_layout.itemAt(0).layout().itemAt(1).widget()

        self.status_lbl = QLabel("等待确认。")
        self.status_lbl.setObjectName("StatusText")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        feedback_layout.addWidget(self.status_lbl)

        self.error_detail = QPlainTextEdit()
        self.error_detail.setObjectName("ErrorDetail")
        self.error_detail.setReadOnly(True)
        self.error_detail.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.error_detail.setMaximumHeight(0)
        self.error_detail.hide()
        feedback_layout.addWidget(self.error_detail)
        bottom.addWidget(feedback_panel, 1)

        body.addLayout(bottom)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addWidget(self._mkbtn("打开设置", "LinkButton", self._reconfig))
        actions.addStretch()
        self.btn_no = self._mkbtn("暂不处理", "SecondaryButton", self.reject)
        self.btn_yes = self._mkbtn("立即同步", "PrimaryButton", self._start_sync)
        actions.addWidget(self.btn_no)
        actions.addWidget(self.btn_yes)
        body.addLayout(actions)

        self._set_status("等待用户确认同步。", state='idle')

    def _reconfig(self):
        logger.info("用户从同步窗口打开设置")
        dlg = SetupDialog(existing=self.config, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            self.config = dlg.config
            self.config_changed = True
            logger.info("同步窗口中的设置已更新")
            self._load_solver_choices_async()

    def _set_solver_loading_state(self, text):
        self.combo_solver.blockSignals(True)
        self.combo_solver.clear()
        self.combo_solver.addItem(text, None)
        self.combo_solver.setEnabled(False)
        self.combo_solver.blockSignals(False)

    def _load_solver_choices_async(self):
        self._set_solver_loading_state("正在加载解决者...")

        issue_id = extract_first_number(self.fields.get('Bug number', ''))
        if not issue_id:
            self._set_solver_loading_state("无法识别 Issue，提交时使用当前登录者")
            return

        self._solver_request_id += 1
        request_id = self._solver_request_id
        loader = SolverChoicesLoader(
            self.config, issue_id, request_id,
            parent=QApplication.instance())
        loader.loaded_sig.connect(self._on_solver_choices_loaded)
        loader.finished.connect(lambda l=loader: self._on_solver_loader_finished(l))
        self._solver_loader = loader
        loader.start()

    def _on_solver_loader_finished(self, loader):
        if self._solver_loader is loader:
            self._solver_loader = None

    def _on_solver_choices_loaded(self, request_id, info):
        if request_id != self._solver_request_id:
            logger.info("忽略过期的解决者候选结果: request_id=%s", request_id)
            return

        current_user_id = info.get('current_user_id')
        current_user_label = info.get('current_user_label') or "当前登录者"
        options = info.get('options') or []
        self.solver_choices = options
        self.default_solver_id = current_user_id

        self.combo_solver.blockSignals(True)
        self.combo_solver.clear()

        if options:
            current_index = -1
            for idx, opt in enumerate(options):
                self.combo_solver.addItem(opt['label'], opt['id'])
                if current_user_id and str(opt['id']) == str(current_user_id):
                    current_index = idx

            if current_index >= 0:
                self.combo_solver.setCurrentIndex(current_index)
                self.combo_solver.setEnabled(True)
            elif current_user_id:
                self.combo_solver.insertItem(0, f"{current_user_label}（当前登录者）", current_user_id)
                self.combo_solver.setCurrentIndex(0)
                self.combo_solver.setEnabled(True)
            else:
                self.combo_solver.insertItem(0, "默认使用当前登录者，也可手动改选", None)
                self.combo_solver.setCurrentIndex(0)
                self.combo_solver.setEnabled(True)
        elif current_user_id:
            self.combo_solver.addItem(f"{current_user_label}（当前登录者）", current_user_id)
            self.combo_solver.setCurrentIndex(0)
            self.combo_solver.setEnabled(False)
        else:
            self.combo_solver.addItem("无法加载解决者列表，提交时尝试使用当前登录者", None)
            self.combo_solver.setEnabled(False)

        self.combo_solver.blockSignals(False)

    def _start_sync(self):
        self.btn_yes.setText("立即同步")
        self.btn_yes.setEnabled(False)
        self.btn_no.setEnabled(False)
        self.error_detail.clear()
        self._toggle_error_detail(False)
        self._set_status("同步中...", state='running')

        # 读取用户手动编辑的值
        comment_val = self.edit_comment.toPlainText().strip() or PLACEHOLDER
        self.fields['Comment'] = comment_val

        hours_text = self.edit_hours.text().strip()
        try:
            hours = float(hours_text) if hours_text else 0.5
        except ValueError:
            logger.warning("工时输入无效，使用默认值 0.5: %s", hours_text)
            hours = 0.5

        status_name = self.combo_status.currentText()
        solver_user_id = self.combo_solver.currentData()
        logger.info("用户确认同步: issue=%s status=%s hours=%s",
                    self.fields.get('Bug number', ''), status_name, hours)

        self.worker = SyncWorker(self.config, self.fields, self.gerrit_url,
                                 hours=hours, status_name=status_name,
                                 solver_user_id=solver_user_id)
        self.worker.log_sig.connect(lambda m: self._set_status(m, state='running'))
        self.worker.finished_sig.connect(self._on_done)
        self.worker.start()

    def _on_done(self, ok, msg):
        if ok:
            self.error_detail.clear()
            self._toggle_error_detail(False)
            logger.info("同步结果成功: %s", msg)
            self._set_status(f"✓  {msg}", state='success')
            self.btn_no.setText("关闭")
            self.btn_no.setEnabled(True)
        else:
            self.error_detail.setPlainText(msg)
            self._toggle_error_detail(True)
            logger.warning("同步结果失败: %s", msg)
            self._set_status("✗  同步失败，详细错误见下方", state='error')
            self.btn_yes.setText("重试")
            self.btn_yes.setEnabled(True)
            self.btn_no.setEnabled(True)
        self.adjustSize()

    def _toggle_error_detail(self, visible):
        if visible:
            self.error_detail.show()
            self.error_detail.setMaximumHeight(0)

        self._error_anim = QPropertyAnimation(self.error_detail, b"maximumHeight", self)
        self._error_anim.setDuration(180)
        self._error_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._error_anim.setStartValue(self.error_detail.maximumHeight())
        self._error_anim.setEndValue(140 if visible else 0)
        if not visible:
            self._error_anim.finished.connect(self.error_detail.hide)
        self._error_anim.start()

    def _set_status(self, text, state='idle'):
        style = self.STATUS_STYLES.get(state, self.STATUS_STYLES['idle'])
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet(
            f"color:{style['text_color']}; font-size:10pt; line-height:1.45em;")
        tint_badge(self.status_pill, *style['badge'])


# ═══════════════════════════════════════════════════════════════════════════════
# 主应用 —— 托盘常驻 + 自动检测
# ═══════════════════════════════════════════════════════════════════════════════
class SyncRedmineApp:
    TOOLTIP_IDLE      = "syncRedmine — 监听提交中..."
    TOOLTIP_DETECTING = "syncRedmine — 等待 push 完成..."
    TOOLTIP_PENDING   = "syncRedmine — 有待同步的提交！"

    def __init__(self, app):
        self.app      = app
        self.config   = load_config()
        self._poller  = None
        self._setup_dialog = None
        self._auto_update_worker = None
        self._auto_update_timer = QTimer()
        self._auto_update_timer.setSingleShot(True)
        self._auto_update_timer.timeout.connect(self._on_auto_update_timeout)
        self._script_path = os.path.abspath(sys.argv[0])
        self._first_run_pending = not bool(self.config)
        self.app.setQuitOnLastWindowClosed(False)
        logger.info("syncRedmine 启动，当前配置状态: %s", "已加载" if self.config else "未配置")

        # ── 托盘图标 ──────────────────────────────────────────────────────────
        self.tray = QSystemTrayIcon(make_icon())
        self.tray.setToolTip(self.TOOLTIP_IDLE)
        menu = QMenu()
        menu.addAction("设置", self._show_setup)
        menu.addSeparator()
        menu.addAction("退  出", app.quit)
        self.tray.setContextMenu(menu)
        self.tray.show()

        # ── inotify 监控 commit_data.log ──────────────────────────────────────
        self._watcher = QFileSystemWatcher()
        self._home_dir = os.path.expanduser('~')
        self._watcher.addPath(self._home_dir)       # 监听目录（捕获文件创建）
        if os.path.exists(DEFAULT_LOG):
            self._watcher.addPath(DEFAULT_LOG)       # 监听文件本身（捕获内容修改）
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.directoryChanged.connect(self._on_dir_changed)
        self._last_mtime = self._get_mtime()         # 记录初始 mtime 防止启动误触

        self._schedule_auto_update()

        # ── 首次运行引导 ──────────────────────────────────────────────────────
        if self._first_run_pending:
            QTimer.singleShot(600, self._first_run)

    def _auto_update_enabled(self):
        return bool(self.config and self.config.get('auto_update_enabled', True))

    def _schedule_auto_update(self):
        self._auto_update_timer.stop()
        if not self._auto_update_enabled():
            logger.info("自动更新未启用，已停止定时任务")
            return

        now = datetime.now()
        target = now.replace(hour=AUTO_UPDATE_HOUR, minute=AUTO_UPDATE_MINUTE, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        delay_ms = max(1000, int((target - now).total_seconds() * 1000))
        self._auto_update_timer.start(delay_ms)
        logger.info("已计划自动更新检查：next=%s delay=%ss", target.strftime('%Y-%m-%d %H:%M:%S'),
                    delay_ms // 1000)

    def _on_auto_update_timeout(self):
        self._schedule_auto_update()
        self._start_auto_update()

    def _start_auto_update(self):
        if not self._auto_update_enabled():
            logger.info("自动更新触发时发现已禁用，跳过")
            return
        if self._auto_update_worker is not None and self._auto_update_worker.isRunning():
            logger.info("自动更新仍在进行中，本次跳过重复执行")
            return

        worker = AutoUpdateWorker(self.config, self._script_path, parent=QApplication.instance())
        worker.finished_sig.connect(self._on_auto_update_done)
        worker.finished.connect(lambda w=worker: self._on_auto_update_worker_finished(w))
        self._auto_update_worker = worker
        worker.start()

    def _on_auto_update_worker_finished(self, worker):
        if self._auto_update_worker is worker:
            self._auto_update_worker = None

    def _on_auto_update_done(self, ok, changed, message):
        if ok and changed:
            logger.info("自动更新完成：%s", message)
            self.tray.showMessage(
                "syncRedmine",
                "检测到新版本，正在重启应用...",
                QSystemTrayIcon.Information, 4000)
            QTimer.singleShot(1200, self._restart_after_update)
        elif ok:
            logger.info("自动更新检查结果：%s", message)
        else:
            logger.warning("自动更新失败：%s", message)
            self.tray.showMessage(
                "syncRedmine",
                f"自动更新失败：{message}",
                QSystemTrayIcon.Warning, 5000)

    def _restart_after_update(self):
        try:
            if self._poller and self._poller.isRunning():
                self._poller.cancel()
                self._poller = None
            subprocess.Popen(
                [sys.executable, self._script_path],
                cwd=os.path.dirname(self._script_path) or None,
                start_new_session=True,
            )
            logger.info("已拉起新进程，准备退出当前实例: %s", self._script_path)
        except Exception as e:
            logger.exception("重启 syncRedmine 失败")
            self.tray.showMessage(
                "syncRedmine",
                f"自动重启失败：{e}",
                QSystemTrayIcon.Warning, 5000)
            return
        self.app.quit()

    # ── inotify 事件处理 ──────────────────────────────────────────────────────
    @staticmethod
    def _get_mtime():
        try:
            return os.path.getmtime(DEFAULT_LOG)
        except FileNotFoundError:
            return 0.0

    def _on_file_changed(self, path):
        """commit_data.log 内容被修改时触发"""
        if os.path.abspath(path) != os.path.abspath(DEFAULT_LOG):
            return
        # 文件可能被 truncate+重写，重新加入监听（inotify 会在某些写法下脱落）
        if DEFAULT_LOG not in self._watcher.files():
            self._watcher.addPath(DEFAULT_LOG)
        self._check_and_fire()

    def _on_dir_changed(self, path):
        """HOME 目录有变动时检查 commit_data.log 是否新建/重写"""
        if not os.path.exists(DEFAULT_LOG):
            return
        # 文件刚出现或被重建，加入监听
        if DEFAULT_LOG not in self._watcher.files():
            self._watcher.addPath(DEFAULT_LOG)
        self._check_and_fire()

    def _check_and_fire(self):
        """比较 mtime，确认是真正的新写入才触发"""
        mtime = self._get_mtime()
        if mtime > self._last_mtime:
            self._last_mtime = mtime
            self._on_log_changed(mtime)

    def _on_log_changed(self, log_mtime):
        fields = parse_commit_log()
        if not fields:
            logger.warning("检测到 commit_data.log 变化，但解析结果为空")
            return
        issue_number = extract_first_number(fields.get('Bug number', ''))
        if not issue_number:
            logger.info("跳过同步: Bug number 无效或为空: %s", fields.get('Bug number', ''))
            return  # NOBUG 等跳过

        topics = get_gerrit_topics(fields)
        if not topics:
            logger.info("跳过同步: 未提取到 Gerrit topics")
            return

        if not self.config:
            logger.warning("检测到新提交但未配置账号，已跳过: issue=%s", issue_number)
            return  # 未配置账号，静默跳过

        logger.info("检测到新提交: issue=%s topics=%s", issue_number, topics)

        # 取消旧的轮询
        if self._poller:
            if self._poller.isRunning():
                self._poller.cancel()
            self._poller = None

        trigger_time = datetime.fromtimestamp(log_mtime, tz=timezone.utc)

        # 启动新的 Gerrit 轮询
        self.tray.setIcon(make_icon('#FF9800'))  # 橙色
        self.tray.setToolTip(self.TOOLTIP_DETECTING)
        self.tray.showMessage(
            "syncRedmine",
            f"检测到新提交 Issue #{issue_number}，等待 push 到 Gerrit...",
            QSystemTrayIcon.Information, 3000)

        poller = GerritPoller(
            self.config, topics,
            trigger_time=trigger_time,
            initial_changes=None)
        poller.finished.connect(poller.deleteLater)
        poller.push_detected.connect(
            lambda url, p=poller, f=fields: self._on_push_detected(p, f, url))
        poller.status_msg.connect(
            lambda m, p=poller: self._on_poller_status(p, m))
        poller.timed_out.connect(lambda p=poller: self._on_poll_timeout(p))
        self._poller = poller
        poller.start()

    def _on_poller_status(self, poller, message):
        if self._poller is poller:
            self.tray.setToolTip(f"syncRedmine — {message}")

    def _on_push_detected(self, poller, fields, gerrit_url):
        if self._poller is not poller:
            logger.info("忽略过期 poller 的 push 通知: gerrit=%s", gerrit_url)
            return
        self._poller = None
        logger.info("检测到 push 完成: issue=%s gerrit=%s",
                    fields.get('Bug number', ''), gerrit_url)
        self.tray.setIcon(make_icon('#4CAF50', badge='!'))  # 绿色+感叹号
        self.tray.setToolTip(self.TOOLTIP_PENDING)
        self.tray.showMessage(
            "syncRedmine",
            f"Push 完成！Issue #{fields.get('Bug number','')}，点击同步到 Redmine",
            QSystemTrayIcon.Information, 5000)
        QTimer.singleShot(500, lambda: self._show_sync(fields, gerrit_url))

    def _on_poll_timeout(self, poller):
        if self._poller is not poller:
            logger.info("忽略过期 poller 的超时信号")
            return
        self._poller = None
        logger.warning("本次 Gerrit 轮询超时，恢复空闲状态")
        self.tray.setIcon(make_icon())
        self.tray.setToolTip(self.TOOLTIP_IDLE)

    @staticmethod
    def _focus_dialog(dialog):
        if dialog is None:
            return
        if dialog.windowState() & Qt.WindowMinimized:
            dialog.setWindowState((dialog.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    # ── 同步对话框 ────────────────────────────────────────────────────────────
    def _show_sync(self, fields, gerrit_url):
        if not self.config:
            self._show_setup()
            if not self.config:
                return
        dlg = SyncDialog(self.config, fields, gerrit_url)
        dlg.exec_()
        if dlg.config_changed:
            self.config = dlg.config
            self._schedule_auto_update()
            logger.info("同步对话框关闭后，主应用设置已同步更新")
        self.tray.setIcon(make_icon())
        self.tray.setToolTip(self.TOOLTIP_IDLE)

    # ── 配置 ──────────────────────────────────────────────────────────────────
    def _show_setup(self):
        self._first_run_pending = False
        if self._setup_dialog is not None:
            logger.info("设置窗口已存在，切换到前台")
            self._focus_dialog(self._setup_dialog)
            return

        logger.info("用户打开设置窗口")
        dlg = SetupDialog(existing=self.config)
        self._setup_dialog = dlg
        try:
            if dlg.exec_() == QDialog.Accepted:
                self.config = dlg.config
                self._schedule_auto_update()
                logger.info("主窗口设置已更新")
        finally:
            if self._setup_dialog is dlg:
                self._setup_dialog = None
            dlg.deleteLater()

    def _first_run(self):
        if not self._first_run_pending or self.config:
            return
        self._first_run_pending = False

        if self._setup_dialog is not None:
            logger.info("首次运行引导触发时设置窗口已打开，切换到前台")
            self._focus_dialog(self._setup_dialog)
            return

        logger.info("首次运行，弹出引导设置")
        QMessageBox.information(
            None, "欢迎使用 syncRedmine",
            "首次使用，请先完成设置。\n\n"
            "可配置 Gerrit / Redmine 账号，\n"
            "以及每天 10:00 自动更新。\n\n"
            "配置完成后程序将在后台运行，\n"
            "每次 commit push 完成后自动弹出同步确认。\n\n"
            "（右键托盘图标可随时打开设置）")
        self._show_setup()


# ═══════════════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setApplicationName("syncRedmine")
    app.setWindowIcon(make_icon())
    app.setStyleSheet(APP_STYLE_SHEET)

    if '--setup' in sys.argv:
        SetupDialog(existing=load_config()).exec_()
        return

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "错误", "系统不支持托盘图标")
        sys.exit(1)

    app._sync_redmine = SyncRedmineApp(app)  # 保持引用，防止 GC 回收
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
