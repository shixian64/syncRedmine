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

import sys, os, re, json, base64, time
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("[syncRedmine] 缺少依赖: pip3 install requests")
    sys.exit(1)

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QGroupBox, QFormLayout, QMessageBox,
    QFrame, QSystemTrayIcon, QMenu,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QFileSystemWatcher
from PyQt5.QtGui import QFont, QIcon, QPixmap, QPainter, QColor

# ═══════════════════════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════════════════════
COMMIT_TOOL_DIR = os.path.join(os.path.expanduser('~'), '.commit_tool')
CONFIG_FILE     = os.path.join(COMMIT_TOOL_DIR, 'sync_config.json')
DEFAULT_LOG     = os.path.join(os.path.expanduser('~'), 'commit_data.log')
PLACEHOLDER     = '请填写'
POLL_INTERVAL   = 4          # 轮询间隔 (秒)
POLL_TIMEOUT    = 180        # 最长等待 push 时间 (秒)
RECENT_PUSH_SLACK = 8        # Gerrit 时间与本机时间允许偏差 (秒)

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
# 配置
# ═══════════════════════════════════════════════════════════════════════════════
def load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        for k in ('gerrit_password', 'redmine_password'):
            if d.get(k):
                d[k] = base64.b64decode(d[k]).decode('utf-8')
        return d
    except Exception:
        return None

def save_config(cfg):
    os.makedirs(COMMIT_TOOL_DIR, exist_ok=True)
    d = dict(cfg)
    for k in ('gerrit_password', 'redmine_password'):
        if d.get(k):
            d[k] = base64.b64encode(d[k].encode('utf-8')).decode('utf-8')
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# commit_data.log 解析
# ═══════════════════════════════════════════════════════════════════════════════
def parse_commit_log(path=DEFAULT_LOG):
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
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

def _gerrit_login(base, username, password):
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
        opener.open(urllib.request.Request(login_url, data=data), timeout=15)
    except urllib.error.HTTPError as e:
        cookie_hdr = e.headers.get("Set-Cookie", "")
        m = re.search(r"GerritAccount=([^;]+)", cookie_hdr)
        if m:
            cookie = m.group(1)
            try:
                with open(GERRIT_COOKIE_CACHE, "w") as f:
                    json.dump({"base_url": base, "cookie": cookie, "time": time.time()}, f)
                os.chmod(GERRIT_COOKIE_CACHE, 0o600)
            except Exception:
                pass
            return cookie
    raise RuntimeError("Gerrit 登录失败：未获取到 cookie")


def _gerrit_api_get(base, cookie, path, params=None):
    """用 cookie 调用 Gerrit REST API"""
    url = f"{base}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url)
    req.add_header("Cookie", f"GerritAccount={cookie}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return _decode_gerrit_json(resp.read().decode("utf-8"))


def fetch_gerrit_changes(config, topics, timeout=10):
    """查询一个或多个 topic 下的 changes（Cookie 认证）。"""
    base = config['gerrit_url'].rstrip('/')
    if isinstance(topics, str):
        topics = [topics]

    try:
        cookie = _gerrit_login(base, config['gerrit_username'], config['gerrit_password'])
    except Exception as e:
        print(f"[syncRedmine] Gerrit 登录失败: {e}")
        return None

    result = {}
    has_success = False

    for topic in topics:
        try:
            changes = _gerrit_api_get(base, cookie, "changes/",
                                      {"q": f"topic:{topic}"})
            has_success = True
            for c in changes:
                num = c.get('_number')
                if num:
                    result[num] = {
                        'updated': c.get('updated', ''),
                        'project': c.get('project', ''),
                    }
        except Exception:
            continue

    return result if has_success else None


def build_gerrit_change_url(base, change_number, change_info=None):
    project = (change_info or {}).get('project', '')
    if project:
        return f"{base}/c/{project}/+/{change_number}"
    return f"{base}/c/+/{change_number}"


# ═══════════════════════════════════════════════════════════════════════════════
# 图标
# ═══════════════════════════════════════════════════════════════════════════════
def make_icon(color='#4a90d9', badge=None):
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 28, 28)
    p.setPen(Qt.white)
    p.setFont(QFont('', 13, QFont.Bold))
    p.drawText(pix.rect(), Qt.AlignCenter, 'R')
    if badge:
        p.setBrush(QColor('#f44336'))
        p.setPen(Qt.NoPen)
        p.drawEllipse(20, 0, 12, 12)
        p.setPen(Qt.white)
        p.setFont(QFont('', 7, QFont.Bold))
        p.drawText(QtCore.QRect(20, 0, 12, 12), Qt.AlignCenter, '!')
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
        self.status_msg.emit("等待 push 完成...")
        initial = self.initial_changes

        # 若建立基线前 push 已完成，则直接识别最近更新的 change
        if initial is not None:
            recent = self._find_recent_change(initial)
            if recent:
                num, info = recent
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
                    self.push_detected.emit(self._build_url(num, info))
                    return
                initial = current
                self.status_msg.emit("已建立 Gerrit 基线，等待 push 完成...")
            else:
                detected = self._detect_change(initial, current)
                if detected:
                    num, info = detected
                    self.push_detected.emit(self._build_url(num, info))
                    return
                self.status_msg.emit(f"等待 push 完成... ({elapsed}s)")

            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

        if not self._stop:
            self.timed_out.emit()


# ═══════════════════════════════════════════════════════════════════════════════
# Redmine 同步线程
# ═══════════════════════════════════════════════════════════════════════════════
class SyncWorker(QThread):
    log_sig      = pyqtSignal(str)
    finished_sig = pyqtSignal(bool, str)

    def __init__(self, config, fields, gerrit_url):
        super().__init__()
        self.config     = config
        self.fields     = fields
        self.gerrit_url = gerrit_url
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
                self.finished_sig.emit(False, f"Bug number 无效")
                return
            issue_id = int(issue_number)

            self.log_sig.emit(f"获取 issue #{issue_id}...")
            r = self._get(f'/issues/{issue_id}.json')
            if r.status_code != 200:
                self.finished_sig.emit(False, f"获取 issue 失败: HTTP {r.status_code}")
                return

            cf_map = {cf['name']: cf['id']
                      for cf in r.json()['issue'].get('custom_fields', [])}

            self.log_sig.emit("获取用户信息...")
            r2   = self._get('/my/account.json')
            uid  = r2.json()['user']['id'] if r2.status_code == 200 else None

            self.log_sig.emit("获取状态列表...")
            r3       = self._get('/issue_statuses.json')
            fixed_id = None
            if r3.status_code == 200:
                for s in r3.json().get('issue_statuses', []):
                    if s['name'].lower() == 'fixed':
                        fixed_id = s['id']
                        break

            # 组装自定义字段
            cfs = []
            if FIX_FIELD_NAME in cf_map:
                cfs.append({'id': cf_map[FIX_FIELD_NAME], 'value': self.gerrit_url or PLACEHOLDER})
            for log_key, rf_name in FIELD_MAP.items():
                val = self.fields.get(log_key, '').strip() or PLACEHOLDER
                if rf_name in cf_map:
                    cfs.append({'id': cf_map[rf_name], 'value': val})
            solver_field_id = cf_map.get(SOLVER_FIELD_NAME)
            if uid and solver_field_id:
                cfs.append({'id': solver_field_id, 'value': str(uid)})
            elif uid and SOLVER_FIELD_NAME not in cf_map:
                self.log_sig.emit(f"提示：未找到自定义字段“{SOLVER_FIELD_NAME}”，已跳过")

            update = {'done_ratio': 100}
            if fixed_id:
                update['status_id'] = fixed_id
            if cfs:
                update['custom_fields'] = cfs

            self.log_sig.emit(f"提交更新 issue #{issue_id}...")
            r4 = self._put(f'/issues/{issue_id}.json', {'issue': update})
            if r4.status_code in (200, 204):
                self.finished_sig.emit(True, f"issue #{issue_id} 同步成功！")
            else:
                self.finished_sig.emit(False,
                    f"更新失败: HTTP {r4.status_code}\n{r4.text[:300]}")
        except Exception as e:
            self.finished_sig.emit(False, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# 账号配置对话框
# ═══════════════════════════════════════════════════════════════════════════════
class SetupDialog(QDialog):
    STYLE_INPUT = ("QLineEdit{border:1px solid #ccc;border-radius:4px;"
                   "padding:4px 6px;font-size:10pt;}"
                   "QLineEdit:focus{border:1.5px solid #4a90d9;}")
    STYLE_GROUP = "QGroupBox{font-weight:bold;font-size:10pt;padding-top:8px;}"

    def __init__(self, existing=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("syncRedmine — 配置平台账号")
        self.setFixedWidth(500)
        self.config = {}
        self._build(existing or {})

    def _le(self, placeholder='', pw=False, val=''):
        e = QLineEdit(val)
        e.setPlaceholderText(placeholder)
        e.setFixedHeight(34)
        e.setStyleSheet(self.STYLE_INPUT)
        if pw:
            e.setEchoMode(QLineEdit.Password)
        return e

    def _build(self, cfg):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(22, 20, 22, 20)

        # ── 标题 ──────────────────────────────────────────────────────────────
        ico = QLabel()
        pix = make_icon('#4a90d9').pixmap(40, 40)
        ico.setPixmap(pix)
        ico.setAlignment(Qt.AlignCenter)
        root.addWidget(ico)

        title = QLabel("配置两个平台的账号密码")
        title.setFont(QFont('', 14, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        root.addWidget(title)

        hint = QLabel("账号信息仅保存在本机  ~/.commit_tool/sync_config.json")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color:#999; font-size:9pt;")
        root.addWidget(hint)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background:#e0e0e0; max-height:1px;")
        root.addWidget(sep)

        # ── Gerrit ────────────────────────────────────────────────────────────
        gb = QGroupBox("  Gerrit  代码审核平台")
        gb.setStyleSheet(self.STYLE_GROUP)
        gf = QFormLayout(gb); gf.setSpacing(10); gf.setContentsMargins(12,12,12,12)
        self.g_url  = self._le('http://...', val=cfg.get('gerrit_url','http://122.227.250.174:8085'))
        self.g_user = self._le('登录用户名', val=cfg.get('gerrit_username',''))
        self.g_pass = self._le('登录密码',   pw=True, val=cfg.get('gerrit_password',''))
        gf.addRow("服务器地址:", self.g_url)
        gf.addRow("用  户  名:", self.g_user)
        gf.addRow("密      码:", self.g_pass)
        root.addWidget(gb)

        # ── Redmine ───────────────────────────────────────────────────────────
        rb = QGroupBox("  Redmine  问题跟踪平台")
        rb.setStyleSheet(self.STYLE_GROUP)
        rf = QFormLayout(rb); rf.setSpacing(10); rf.setContentsMargins(12,12,12,12)
        self.r_url  = self._le('http://...', val=cfg.get('redmine_url','http://122.227.250.174:8078'))
        self.r_user = self._le('登录用户名', val=cfg.get('redmine_username',''))
        self.r_pass = self._le('登录密码',   pw=True, val=cfg.get('redmine_password',''))
        rf.addRow("服务器地址:", self.r_url)
        rf.addRow("用  户  名:", self.r_user)
        rf.addRow("密      码:", self.r_pass)
        root.addWidget(rb)

        # ── 按钮 ──────────────────────────────────────────────────────────────
        row = QHBoxLayout(); row.setSpacing(10)
        row.addWidget(self._mkbtn("保存配置", '#4a90d9', self._save))
        row.addWidget(self._mkbtn("取  消",   '#9e9e9e', self.reject))
        root.addLayout(row)

    @staticmethod
    def _mkbtn(text, color, slot):
        b = QPushButton(text)
        b.setFixedHeight(40)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton{{background:{color};color:white;border:none;"
            f"border-radius:5px;font-size:11pt;}}"
            f"QPushButton:hover{{opacity:0.85;}}"
            f"QPushButton:pressed{{padding-top:2px;}}")
        b.clicked.connect(slot)
        return b

    def _save(self):
        if not self.g_user.text().strip():
            QMessageBox.warning(self, "提示", "请输入 Gerrit 用户名"); return
        if not self.g_pass.text().strip():
            QMessageBox.warning(self, "提示", "请输入 Gerrit 密码"); return
        if not self.r_user.text().strip():
            QMessageBox.warning(self, "提示", "请输入 Redmine 用户名"); return
        if not self.r_pass.text().strip():
            QMessageBox.warning(self, "提示", "请输入 Redmine 密码"); return
        self.config = {
            'gerrit_url':      self.g_url.text().strip(),
            'gerrit_username': self.g_user.text().strip(),
            'gerrit_password': self.g_pass.text().strip(),
            'redmine_url':     self.r_url.text().strip(),
            'redmine_username':self.r_user.text().strip(),
            'redmine_password':self.r_pass.text().strip(),
        }
        save_config(self.config)
        QMessageBox.information(self, "成功", "账号配置已保存 ✓")
        self.accept()


# ═══════════════════════════════════════════════════════════════════════════════
# 同步确认对话框
# ═══════════════════════════════════════════════════════════════════════════════
class SyncDialog(QDialog):
    CARD_STYLE = ("QGroupBox{background:#f8f9fa;border:1px solid #e0e0e0;"
                  "border-radius:6px;padding-top:10px;font-weight:bold;font-size:10pt;}")

    def __init__(self, config, fields, gerrit_url, parent=None):
        super().__init__(parent)
        self.config     = config
        self.fields     = fields
        self.gerrit_url = gerrit_url
        self.worker     = None
        self.setWindowTitle("syncRedmine — 同步提交信息到 Redmine")
        self.setFixedWidth(580)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(20, 18, 20, 18)

        # ── 标题行 ────────────────────────────────────────────────────────────
        hrow = QHBoxLayout()
        ico_lbl = QLabel()
        ico_lbl.setPixmap(make_icon('#4CAF50', badge='!').pixmap(36, 36))
        hrow.addWidget(ico_lbl)
        t = QLabel("检测到提交完成，是否同步到 Redmine？")
        t.setFont(QFont('', 12, QFont.Bold))
        hrow.addWidget(t, 1)
        root.addLayout(hrow)

        # ── 提交信息卡片 ───────────────────────────────────────────────────────
        card = QGroupBox("提交信息预览")
        card.setStyleSheet(self.CARD_STYLE)
        fl = QFormLayout(card)
        fl.setSpacing(7)
        fl.setContentsMargins(14, 10, 14, 12)

        def add_row(k, v, maxlen=80):
            v = (v or '-')
            lbl = QLabel(v[:maxlen] + ('…' if len(v) > maxlen else ''))
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color:#333;")
            key_lbl = QLabel(k)
            key_lbl.setStyleSheet("color:#555; font-weight:bold;")
            fl.addRow(key_lbl, lbl)

        add_row("Issue:",    f"#{self.fields.get('Bug number', '-')}")
        add_row("提交者:",   self.fields.get('Author', '-'))
        add_row("问题根源:", self.fields.get('Root Cause') or PLACEHOLDER)
        add_row("修复方案:", self.fields.get('Solution') or PLACEHOLDER)

        gerrit_display = (self.gerrit_url or f'(未获取到，将填写"{PLACEHOLDER}")')
        url_lbl = QLabel(f'<a href="{self.gerrit_url}" style="color:#1565c0;">'
                         f'{gerrit_display[:80]}</a>')
        url_lbl.setOpenExternalLinks(True)
        url_lbl.setWordWrap(True)
        url_lbl.setStyleSheet("color:#1565c0;")
        fl.addRow(QLabel('<b style="color:#555;">Gerrit:</b>'), url_lbl)
        root.addWidget(card)

        # ── 将更新字段说明 ────────────────────────────────────────────────────
        tags = ["状态→Fixed", "完成度→100%", "解决者", "修复情况",
                "问题根源", "修复方案", "自测情况", "建议", "查找问题的思路"]
        tag_html = " ".join(
            f'<span style="background:#e3f2fd;color:#1565c0;border-radius:3px;'
            f'padding:2px 6px;font-size:9pt;">{t}</span>' for t in tags)
        note = QLabel(f"将更新: {tag_html}")
        note.setWordWrap(True)
        note.setTextFormat(Qt.RichText)
        root.addWidget(note)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background:#e0e0e0; max-height:1px;")
        root.addWidget(sep)

        # ── 状态标签 ──────────────────────────────────────────────────────────
        self.status_lbl = QLabel("")
        self.status_lbl.setAlignment(Qt.AlignCenter)
        self.status_lbl.setMinimumHeight(28)
        self.status_lbl.setStyleSheet("font-size:10pt;")
        root.addWidget(self.status_lbl)

        # ── 按钮行 ────────────────────────────────────────────────────────────
        brow = QHBoxLayout(); brow.setSpacing(10)
        self.btn_yes = self._mkbtn("是，立即同步", '#4CAF50', self._start_sync)
        self.btn_no  = self._mkbtn("否，跳过",     '#9e9e9e', self.reject)
        brow.addWidget(self.btn_yes)
        brow.addWidget(self.btn_no)
        root.addLayout(brow)

        # ── 底部小链接 ────────────────────────────────────────────────────────
        cfg_lnk = QLabel('<a href="s" style="color:#bbb;font-size:8pt;">重新配置账号</a>')
        cfg_lnk.setAlignment(Qt.AlignRight)
        cfg_lnk.linkActivated.connect(lambda _: self._reconfig())
        root.addWidget(cfg_lnk)

    @staticmethod
    def _mkbtn(text, color, slot):
        b = QPushButton(text)
        b.setFixedHeight(42)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton{{background:{color};color:white;border:none;"
            f"border-radius:5px;font-size:11pt;font-weight:bold;}}"
            f"QPushButton:hover{{opacity:0.85;}}"
            f"QPushButton:disabled{{background:#bdbdbd;color:#888;}}")
        b.clicked.connect(slot)
        return b

    def _reconfig(self):
        dlg = SetupDialog(existing=self.config, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            self.config = dlg.config

    def _start_sync(self):
        self.btn_yes.setEnabled(False)
        self.btn_no.setEnabled(False)
        self._set_status("同步中...", "#2196F3")
        self.worker = SyncWorker(self.config, self.fields, self.gerrit_url)
        self.worker.log_sig.connect(lambda m: self._set_status(m, "#2196F3"))
        self.worker.finished_sig.connect(self._on_done)
        self.worker.start()

    def _on_done(self, ok, msg):
        if ok:
            self._set_status(f"✓  {msg}", "#4CAF50")
            self.btn_no.setText("关 闭")
            self.btn_no.setEnabled(True)
            self.btn_no.setStyleSheet(
                "QPushButton{background:#2196F3;color:white;border:none;"
                "border-radius:5px;font-size:11pt;font-weight:bold;}")
        else:
            self._set_status(f"✗  {msg}", "#f44336")
            self.btn_yes.setText("重 试")
            self.btn_yes.setEnabled(True)
            self.btn_no.setEnabled(True)

    def _set_status(self, text, color):
        self.status_lbl.setStyleSheet(f"font-size:10pt; color:{color};")
        self.status_lbl.setText(text)


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
        self.app.setQuitOnLastWindowClosed(False)

        # ── 托盘图标 ──────────────────────────────────────────────────────────
        self.tray = QSystemTrayIcon(make_icon())
        self.tray.setToolTip(self.TOOLTIP_IDLE)
        menu = QMenu()
        menu.addAction("配置账号", self._show_setup)
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

        # ── 首次运行引导 ──────────────────────────────────────────────────────
        if not self.config:
            QTimer.singleShot(600, self._first_run)

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
            return
        issue_number = extract_first_number(fields.get('Bug number', ''))
        if not issue_number:
            return  # NOBUG 等跳过

        topics = get_gerrit_topics(fields)
        if not topics:
            return

        if not self.config:
            return  # 未配置账号，静默跳过

        # 取消旧的轮询
        if self._poller and self._poller.isRunning():
            self._poller.cancel()

        trigger_time = datetime.fromtimestamp(log_mtime, tz=timezone.utc)
        initial_changes = fetch_gerrit_changes(self.config, topics, timeout=2)

        # 启动新的 Gerrit 轮询
        self.tray.setIcon(make_icon('#FF9800'))  # 橙色
        self.tray.setToolTip(self.TOOLTIP_DETECTING)
        self.tray.showMessage(
            "syncRedmine",
            f"检测到新提交 Issue #{issue_number}，等待 push 到 Gerrit...",
            QSystemTrayIcon.Information, 3000)

        self._poller = GerritPoller(
            self.config, topics,
            trigger_time=trigger_time,
            initial_changes=initial_changes)
        self._poller.push_detected.connect(
            lambda url: self._on_push_detected(fields, url))
        self._poller.status_msg.connect(
            lambda m: self.tray.setToolTip(f"syncRedmine — {m}"))
        self._poller.timed_out.connect(self._on_poll_timeout)
        self._poller.start()

    def _on_push_detected(self, fields, gerrit_url):
        self.tray.setIcon(make_icon('#4CAF50', badge='!'))  # 绿色+感叹号
        self.tray.setToolTip(self.TOOLTIP_PENDING)
        self.tray.showMessage(
            "syncRedmine",
            f"Push 完成！Issue #{fields.get('Bug number','')}，点击同步到 Redmine",
            QSystemTrayIcon.Information, 5000)
        QTimer.singleShot(500, lambda: self._show_sync(fields, gerrit_url))

    def _on_poll_timeout(self):
        self.tray.setIcon(make_icon())
        self.tray.setToolTip(self.TOOLTIP_IDLE)

    # ── 同步对话框 ────────────────────────────────────────────────────────────
    def _show_sync(self, fields, gerrit_url):
        if not self.config:
            self._show_setup()
            if not self.config:
                return
        dlg = SyncDialog(self.config, fields, gerrit_url)
        dlg.exec_()
        self.config = dlg.config
        self.tray.setIcon(make_icon())
        self.tray.setToolTip(self.TOOLTIP_IDLE)

    # ── 配置 ──────────────────────────────────────────────────────────────────
    def _show_setup(self):
        dlg = SetupDialog(existing=self.config)
        if dlg.exec_() == QDialog.Accepted:
            self.config = dlg.config

    def _first_run(self):
        QMessageBox.information(
            None, "欢迎使用 syncRedmine",
            "首次使用，请配置 Gerrit 和 Redmine 账号。\n\n"
            "配置完成后程序将在后台运行，\n"
            "每次 commit push 完成后自动弹出同步确认。\n\n"
            "（右键托盘图标可随时重新配置）")
        self._show_setup()


# ═══════════════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setApplicationName("syncRedmine")

    if '--setup' in sys.argv:
        SetupDialog(existing=load_config()).exec_()
        return

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "错误", "系统不支持托盘图标")
        sys.exit(1)

    instance = SyncRedmineApp(app)  # 必须保持引用，防止 GC 回收
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
