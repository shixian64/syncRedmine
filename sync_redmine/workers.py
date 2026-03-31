# -*- coding: utf-8 -*-
"""QThread 后台工作线程：Gerrit 轮询、Redmine 同步、解决者加载、自动更新。"""

import os, sys, json, time, subprocess, tempfile, shutil

import requests
from PyQt5.QtCore import QThread, pyqtSignal
from datetime import datetime, timedelta, timezone

from .constants import (
    POLL_INTERVAL, POLL_TIMEOUT, RECENT_PUSH_SLACK,
    PLACEHOLDER, FIELD_MAP, FIX_FIELD_NAME, SOLVER_FIELD_NAME,
    GITHUB_DEFAULT_BRANCH, VERSION_FILE,
    logger,
)
from .api import (
    extract_first_number, parse_gerrit_time,
    fetch_gerrit_changes, build_gerrit_change_url,
    fetch_redmine_solver_choices,
)


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


# ═══════════════════════════════════════════════════════════════════════════════
# 自动更新线程（GitHub）
# ═══════════════════════════════════════════════════════════════════════════════
class AutoUpdateWorker(QThread):
    finished_sig = pyqtSignal(bool, bool, str)  # ok, changed, message

    def __init__(self, config, local_script_path, parent=None):
        super().__init__(parent)
        self.config = dict(config or {})
        self.local_script_path = os.path.abspath(local_script_path)

    REQUIRED_FILES = ('syncRedmine.py', 'install.sh')

    @staticmethod
    def _read_local_version():
        try:
            if os.path.isfile(VERSION_FILE):
                with open(VERSION_FILE, 'r') as f:
                    return f.read().strip()
        except OSError:
            pass
        return ''

    @staticmethod
    def _write_local_version(sha):
        os.makedirs(os.path.dirname(VERSION_FILE), exist_ok=True)
        with open(VERSION_FILE, 'w') as f:
            f.write(sha)

    def run(self):
        repo = (self.config.get('github_repo') or '').strip()
        branch = (self.config.get('github_branch') or '').strip() or GITHUB_DEFAULT_BRANCH

        if not repo:
            logger.info("自动更新跳过：未配置 GitHub 仓库")
            self.finished_sig.emit(True, False, "自动更新未配置 GitHub 仓库，已跳过")
            return

        tmp_dir = None
        try:
            # ── 1. 查询最新 commit SHA ────────────────────────────────────
            api_url = f"https://api.github.com/repos/{repo}/commits/{branch}"
            logger.info("开始自动更新检查：repo=%s branch=%s", repo, branch)
            resp = requests.get(api_url, headers={'Accept': 'application/vnd.github.v3+json'}, timeout=15)
            if resp.status_code == 404:
                raise RuntimeError(f"GitHub 仓库或分支不存在：{repo} ({branch})")
            resp.raise_for_status()
            latest_sha = resp.json()['sha']
            local_sha = self._read_local_version()
            logger.info("版本比较：local=%s remote=%s", local_sha[:8] if local_sha else '(无)', latest_sha[:8])

            if latest_sha == local_sha:
                logger.info("自动更新检查完成：当前已是最新版本")
                self.finished_sig.emit(True, False, "已检查更新，无新版本")
                return

            # ── 2. 下载仓库 zip 包 ────────────────────────────────────────
            zip_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
            logger.info("检测到新版本，正在下载：%s", zip_url)
            zip_resp = requests.get(zip_url, timeout=120, stream=True)
            zip_resp.raise_for_status()

            tmp_dir = tempfile.mkdtemp(prefix='syncRedmine_update_')
            zip_path = os.path.join(tmp_dir, 'repo.zip')
            with open(zip_path, 'wb') as f:
                for chunk in zip_resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info("下载完成，正在解压：%s", zip_path)

            import zipfile
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(tmp_dir)

            # zip 解压后的目录名格式: {repo_name}-{branch}/
            extracted_dirs = [d for d in os.listdir(tmp_dir) if os.path.isdir(os.path.join(tmp_dir, d))]
            if not extracted_dirs:
                raise RuntimeError("解压后未找到源码目录")
            src_dir = os.path.join(tmp_dir, extracted_dirs[0])

            for fname in self.REQUIRED_FILES:
                if not os.path.isfile(os.path.join(src_dir, fname)):
                    raise RuntimeError(f"仓库中找不到必需文件：{fname}")
            logger.info("源码解压完成：%s", src_dir)

            # ── 3. 执行 install.sh 完成安装 ───────────────────────────────
            install_sh = os.path.join(src_dir, 'install.sh')
            os.chmod(install_sh, 0o755)
            env = {**os.environ, 'AUTO_INSTALL': '1'}
            result = subprocess.run(
                ['bash', install_sh],
                cwd=src_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or '').strip()
                raise RuntimeError(f"install.sh 返回错误 (code={result.returncode}):\n{detail}")

            # ── 4. 记录版本号 ─────────────────────────────────────────────
            self._write_local_version(latest_sha)
            logger.info("自动更新成功：%s → %s", local_sha[:8] if local_sha else '(无)', latest_sha[:8])
            self.finished_sig.emit(True, True,
                f"已从 GitHub 同步到最新版本 ({latest_sha[:8]})")

        except Exception as e:
            logger.exception("自动更新失败")
            self.finished_sig.emit(False, False, str(e))
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)


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
