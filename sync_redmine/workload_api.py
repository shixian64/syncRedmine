# -*- coding: utf-8 -*-
"""PM 工时系统 API 封装 + Redmine 活动页面解析。"""

import re
import json
import threading
from datetime import datetime, timedelta

import requests

from .constants import logger


_PM_AUTH_CACHE = {}
_PM_AUTH_LOCK = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# PM 系统会话
# ═══════════════════════════════════════════════════════════════════════════════
class PMSession:
    """PM 工时系统会话管理，基于 requests.Session 维护 cookie。"""

    def __init__(self, base_url, username, password):
        self.base = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.user_id = None
        self.user_nick = None
        self.token = None
        self._restore_cached_auth()

    def _cache_key(self):
        return (self.base, self.username, self.password)

    def _restore_cached_auth(self):
        """从进程内缓存恢复 PM 登录态，减少重复登录。"""
        with _PM_AUTH_LOCK:
            cached = _PM_AUTH_CACHE.get(self._cache_key())

        if not cached:
            return False

        self.user_id = cached.get('user_id')
        self.user_nick = cached.get('user_nick')
        self.token = cached.get('token')
        cookies = cached.get('cookies') or {}
        if self.token:
            self.session.headers.update({'Authorization': self.token})
        if cookies:
            self.session.cookies.update(cookies)
        logger.debug("复用 PM 登录态: userId=%s nick=%s", self.user_id, self.user_nick)
        return True

    def _save_cached_auth(self):
        """保存当前登录态到进程内缓存。"""
        with _PM_AUTH_LOCK:
            _PM_AUTH_CACHE[self._cache_key()] = {
                'user_id': self.user_id,
                'user_nick': self.user_nick,
                'token': self.token,
                'cookies': requests.utils.dict_from_cookiejar(self.session.cookies),
            }

    def _clear_cached_auth(self):
        """清理本地及进程内缓存的登录态。"""
        with _PM_AUTH_LOCK:
            _PM_AUTH_CACHE.pop(self._cache_key(), None)
        self.user_id = None
        self.user_nick = None
        self.token = None
        self.session.headers.pop('Authorization', None)
        self.session.cookies.clear()

    def login(self, force=False):
        """POST /pmSystemApi/admin/login → 返回 {userId, userNick, ...}"""
        if not force and self.token and self.user_id:
            return {
                'code': 200,
                'msg': 'reuse cached login',
                'userId': self.user_id,
                'nickName': self.user_nick,
                'token': self.token,
            }

        resp = self.session.post(
            f'{self.base}/pmSystemApi/admin/login',
            json={'userName': self.username, 'password': self.password, 'rememberMe': False},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get('code') != 200:
            raise RuntimeError(f"PM 登录失败: {data.get('msg', '未知错误')}")
        # 登录响应: {code, msg, userId, nickName, token, permissionList}
        self.user_id = data.get('userId')
        self.user_nick = data.get('nickName') or self.username
        self.token = data.get('token')
        if self.token:
            self.session.headers.update({'Authorization': self.token})
        self._save_cached_auth()
        logger.info("PM 系统登录成功: userId=%s nick=%s", self.user_id, self.user_nick)
        return data

    def post(self, path, body=None, form=False):
        """带自动重登录的 POST 请求。

        form=True 时使用 application/x-www-form-urlencoded 格式，
        form=False 时使用 application/json 格式。
        """
        if not self.token:
            self.login()

        url = f'{self.base}/pmSystemApi/admin{path}'
        kw = dict(data=body, timeout=15) if form else dict(json=body or {}, timeout=15)
        resp = self.session.post(url, **kw)
        resp.raise_for_status()
        data = resp.json()
        if data.get('code') == 401:
            logger.info("PM session 过期，尝试重新登录")
            self._clear_cached_auth()
            self.login(force=True)
            resp = self.session.post(url, **kw)
            resp.raise_for_status()
            data = resp.json()
        if data.get('code') != 200:
            raise RuntimeError(f"PM API {path} 失败: {data.get('msg', '未知错误')}")
        return data


# ═══════════════════════════════════════════════════════════════════════════════
# PM 系统下拉选项获取
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_development_projects(session):
    """获取开发项目列表（含关联的 businessDepartment 和 productForm）。"""
    data = session.post('/userWorkloadData/getPMSAndReProjectDataByUserId', {})
    return data.get('developmentProject') or []


def fetch_common_projects(session):
    """获取 common 项目列表。"""
    data = session.post('/commonProject/lists', {})
    return data.get('lists') or []


def fetch_work_modules(session, user_id):
    """获取工作模块列表。"""
    data = session.post('/userWorkloadData/getWorkModuleDataByUserId',
                         {'userId': user_id}, form=True)
    return data.get('workModules') or []


def fetch_work_sub_modules_by_module(session, work_module_id):
    """按 workModuleId 获取子模块列表（与网页 API 一致）。"""
    data = session.post('/userWorkloadData/getSubWorkModuleDataByWorkModuleId',
                         {'workModuleId': work_module_id}, form=True)
    return data.get('workSubModules') or []


def fetch_npi_nodes(session):
    """获取 NPI 节点列表。"""
    data = session.post('/userWorkloadData/getNPINode', None, form=True)
    nodes = data.get('npiNodeList') or []
    nodes.sort(key=lambda x: x.get('sortIndex', 0))
    return nodes


def fetch_product_forms(session):
    """获取产品形式列表。"""
    data = session.post('/productForm/lists', {})
    return data.get('lists') or []


def fetch_check_persons(session, user_id):
    """获取检查人列表。"""
    data = session.post('/userWorkloadData/getCheckPersonDataByUserId',
                         {'userId': user_id}, form=True)
    return data.get('checkPersons') or []


def fetch_history_records(session, user_id, days=90):
    """获取最近工时记录（用于提取子模块映射和项目类别）。"""
    now = datetime.now()
    start = (now - timedelta(days=days)).strftime('%Y-%m-%d 00:00:00')
    end = now.strftime('%Y-%m-%d 23:59:59')
    data = session.post('/workloadRecord/listDtoByPage', {
        'currPage': 1,
        'pageSize': 50,
        'dataForm': {
            'creatorId': user_id,
            'workTimes': [start, end],
        },
    })
    return data.get('record') or data.get('records') or []


def fetch_previous_day_record(session, user_id):
    """查询前一天（自然日）的工时记录，返回最后一条。"""
    yesterday = datetime.now() - timedelta(days=1)
    start = yesterday.strftime('%Y-%m-%d 00:00:00')
    end = yesterday.strftime('%Y-%m-%d 23:59:59')
    data = session.post('/workloadRecord/listDtoByPage', {
        'currPage': 1,
        'pageSize': 50,
        'dataForm': {
            'creatorId': user_id,
            'workTimes': [start, end],
        },
    })
    records = data.get('record') or data.get('records') or []
    if not records:
        return None
    return records[-1]


def record_to_defaults(record):
    """把 PM 工时记录转为 workload_defaults 格式 dict。"""
    if not record:
        return None

    common = record.get('commonProject') or {}
    work_hour = record.get('workHour')
    if work_hour in (None, ''):
        work_hour = '7'

    return {
        'workloadType': str(record.get('workloadType', '1')),
        'preResearchProjectId': str(record.get('preResearchProjectId') or ''),
        'projectCategory': record.get('projectCategory') or '',
        'commonProjectId': str(record.get('commonProjectId') or ''),
        'outerProjectCategory': (
            record.get('outerProjectCategory')
            or common.get('commonProjectName')
            or common.get('commonProjectDescription')
            or ''
        ),
        'businessDepartment': str(record.get('businessDepartment') or ''),
        'workModuleId': str(record.get('workModuleId') or ''),
        'workSubModuleId': str(record.get('workSubModuleId') or ''),
        'workloadNpiNode': record.get('workloadNpiNode') or '',
        'productForm': record.get('productForm') or '',
        'inspectorId': str(record.get('inspectorId') or ''),
        'workContent': record.get('workContent') or '',
        'workHour': str(work_hour),
        'remark': record.get('remark') or '',
    }


def build_sub_module_map(records):
    """从历史记录构建 {workModuleId: [{id, name}]} 映射。"""
    mapping = {}
    seen = set()
    for rec in records:
        sub = rec.get('workSubModule') or {}
        sub_id = sub.get('workSubModuleId')
        mod_id = sub.get('workModuleId') or rec.get('workModuleId')
        if not sub_id or not mod_id:
            continue
        key = (mod_id, sub_id)
        if key in seen:
            continue
        seen.add(key)
        mapping.setdefault(mod_id, []).append({
            'id': sub_id,
            'name': sub.get('workSubModuleName') or str(sub_id),
        })
    return mapping


def extract_project_categories(records):
    """从历史记录中提取去重的 projectCategory 列表。"""
    cats = []
    seen = set()
    for rec in records:
        cat = rec.get('projectCategory') or ''
        if cat and cat not in seen:
            seen.add(cat)
            cats.append(cat)
    return cats


def extract_business_departments(records):
    """从历史记录中提取去重的 businessDepartment 列表。"""
    deps = []
    seen = set()
    for rec in records:
        dep = str(rec.get('businessDepartment') or '')
        if dep and dep not in seen:
            seen.add(dep)
            deps.append(dep)
    return deps


# ═══════════════════════════════════════════════════════════════════════════════
# 工时提交
# ═══════════════════════════════════════════════════════════════════════════════
def submit_workload(session, payload):
    """POST /workloadRecord/insertOrUpdate 提交工时。"""
    data = session.post('/workloadRecord/insertOrUpdate', payload)
    return data


# ═══════════════════════════════════════════════════════════════════════════════
# Redmine 活动页面解析
# ═══════════════════════════════════════════════════════════════════════════════
_RE_DT = re.compile(
    r'<dt\b[^>]*>\s*'
    r'<span class="time">([^<]+)</span>\s*'
    r'<span class="project">([^<]+)</span>\s*'
    r'<a href="/issues/(\d+)[^"]*">\s*'
    r'(.*?)\s*</a>',
    re.DOTALL,
)

_RE_H3 = re.compile(r'<h3>(.+?)</h3>', re.DOTALL)


def fetch_redmine_activities(redmine_url, username, password, user_id, date=None):
    """获取指定日期的 Redmine 活动列表。"""
    if date is None:
        date = datetime.now().strftime('%Y-%m-%d')
    url = f'{redmine_url.rstrip("/")}/activity?from={date}&user_id={user_id}'
    resp = requests.get(url, auth=(username, password), timeout=15)
    resp.raise_for_status()
    return parse_redmine_activities(resp.text, date)


def parse_redmine_activities(html, target_date=None):
    """解析 Redmine /activity HTML 页面，提取活动条目。

    仅提取"今天"日期分组内的条目（若 target_date 为今天），
    否则按日期匹配。返回按 issue_id 去重的列表。
    """
    if target_date is None:
        target_date = datetime.now().strftime('%Y-%m-%d')

    today = datetime.now().strftime('%Y-%m-%d')
    target_month_day = None
    try:
        dt = datetime.strptime(target_date, '%Y-%m-%d')
        target_month_day = dt.strftime('%m/%d')
    except ValueError:
        pass

    # 按 <h3> 分割内容，找到目标日期的区块
    sections = _RE_H3.split(html)
    target_html = ''
    for i in range(1, len(sections), 2):
        header = sections[i].strip()
        content = sections[i + 1] if i + 1 < len(sections) else ''
        if target_date == today and '今天' in header:
            target_html = content
            break
        if target_month_day and target_month_day in header:
            target_html = content
            break
        if target_date in header:
            target_html = content
            break

    if not target_html:
        # fallback: 搜索整个页面
        target_html = html

    activities = []
    seen_issues = set()
    for m in _RE_DT.finditer(target_html):
        time_str, project, issue_id, link_text = m.groups()
        issue_id = int(issue_id)
        if issue_id in seen_issues:
            continue
        seen_issues.add(issue_id)

        # 解析标题：Software_SR2 #409126 (OnGoing): [BUG]屏保默认选项为null
        title = link_text.strip()
        # 提取冒号后的简短标题
        colon_idx = title.find(':')
        short_title = title[colon_idx + 1:].strip() if colon_idx >= 0 else title

        activities.append({
            'time': time_str.strip(),
            'project': project.strip(),
            'issue_id': issue_id,
            'title': short_title,
            'full_title': title,
        })

    return activities
