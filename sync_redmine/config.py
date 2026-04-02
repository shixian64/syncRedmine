# -*- coding: utf-8 -*-
"""配置文件加载与保存。"""

import os, json, base64

from .constants import (
    CONFIG_FILE, COMMIT_TOOL_DIR,
    GITHUB_DEFAULT_REPO, GITHUB_DEFAULT_BRANCH,
    logger,
)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        for k in ('gerrit_password', 'redmine_password', 'pm_password'):
            if d.get(k):
                d[k] = base64.b64decode(d[k]).decode('utf-8')
        if 'auto_update_enabled' not in d:
            d['auto_update_enabled'] = True
        # 从旧版 SSH 配置迁移到 GitHub 配置
        if not d.get('github_repo'):
            d['github_repo'] = GITHUB_DEFAULT_REPO
        if not d.get('github_branch'):
            d['github_branch'] = GITHUB_DEFAULT_BRANCH
        # 清除旧版 SSH 字段
        for old_key in ('update_host', 'update_port', 'update_username',
                        'update_password', 'update_repo_path', 'update_remote_path'):
            d.pop(old_key, None)
        return d
    except Exception:
        return None


def save_config(cfg):
    os.makedirs(COMMIT_TOOL_DIR, exist_ok=True)
    d = dict(cfg)
    for k in ('gerrit_password', 'redmine_password', 'pm_password'):
        if d.get(k):
            d[k] = base64.b64encode(d[k].encode('utf-8')).decode('utf-8')
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    logger.info("账号配置已保存: %s", CONFIG_FILE)
