# -*- coding: utf-8 -*-
"""Gerrit / Redmine API 交互函数。"""

import os, re, json, time
import urllib.request, urllib.parse, urllib.error
from html import unescape
from datetime import datetime, timezone

import requests

from .constants import DEFAULT_LOG, PLACEHOLDER, logger


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
    """从 Redmine issue/edit 页面中解析"解决者"下拉候选项。"""
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
    """获取当前登录用户与"解决者"候选列表。"""
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
