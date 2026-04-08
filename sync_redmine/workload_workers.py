# -*- coding: utf-8 -*-
"""工时提交相关的后台线程。"""

from PyQt5.QtCore import QThread, pyqtSignal

from .constants import logger
from .workload_api import (
    PMSession,
    fetch_development_projects, fetch_common_projects,
    fetch_work_modules, fetch_work_sub_modules_by_module,
    fetch_npi_nodes, fetch_product_forms, fetch_check_persons,
    fetch_history_records, fetch_previous_day_record, record_to_defaults,
    extract_project_categories,
    submit_workload, fetch_redmine_activities,
)


class WorkloadDropdownLoader(QThread):
    """加载 PM 系统所有下拉选项 + 历史记录。"""

    loaded_sig = pyqtSignal(dict)
    error_sig = pyqtSignal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config

    def run(self):
        try:
            pm_url = self.config.get('pm_url', '')
            pm_user = self.config.get('pm_username', '')
            pm_pass = self.config.get('pm_password', '')

            session = PMSession(pm_url, pm_user, pm_pass)
            user_info = session.login()
            user_id = session.user_id

            # 获取开发项目列表（含关联的 businessDepartment/productForm）
            dev_projects = []
            try:
                dev_projects = fetch_development_projects(session)
            except Exception:
                logger.debug("fetch_development_projects 失败，回退到历史记录")

            common_projects = []
            try:
                common_projects = fetch_common_projects(session)
            except Exception:
                logger.debug("fetch_common_projects 失败")

            modules = fetch_work_modules(session, user_id)
            npi_nodes = fetch_npi_nodes(session)
            product_forms = fetch_product_forms(session)
            # 检查人：传当前登录用户的 userId（与网页一致，不同用户有不同检查人）
            check_persons = fetch_check_persons(session, user_id)
            records = fetch_history_records(session, user_id, days=90)
            project_categories = extract_project_categories(records)

            prev_day_record = None
            try:
                prev_day_record = fetch_previous_day_record(session, user_id)
            except Exception:
                logger.debug("查询前一天工时记录失败，将使用本地默认值")
            prev_day_defaults = record_to_defaults(prev_day_record) if prev_day_record else None

            self.loaded_sig.emit({
                'dev_projects': dev_projects,
                'common_projects': common_projects,
                'modules': modules,
                'npi_nodes': npi_nodes,
                'product_forms': product_forms,
                'check_persons': check_persons,
                'project_categories': project_categories,
                'prev_day_defaults': prev_day_defaults,
                'user_id': user_id,
                'user_nick': session.user_nick,
                'pm_session_base': pm_url,
                'pm_session_user': pm_user,
                'pm_session_pass': pm_pass,
            })
        except Exception as e:
            logger.exception("加载工时下拉选项失败")
            self.error_sig.emit(str(e))


class RedmineActivityLoader(QThread):
    """获取今日 Redmine 活动。"""

    loaded_sig = pyqtSignal(list)
    error_sig = pyqtSignal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config

    def run(self):
        try:
            redmine_url = self.config.get('redmine_url', '')
            username = self.config.get('redmine_username', '')
            password = self.config.get('redmine_password', '')
            user_id = self.config.get('redmine_user_id', '')

            if not all([redmine_url, username, password, user_id]):
                self.error_sig.emit("Redmine 配置不完整，无法获取活动")
                return

            activities = fetch_redmine_activities(redmine_url, username, password, user_id)
            self.loaded_sig.emit(activities)
        except Exception as e:
            logger.exception("获取 Redmine 活动失败")
            self.error_sig.emit(str(e))


class SubModuleLoader(QThread):
    """按 workModuleId 加载子模块列表（与网页行为一致：选择模块后触发）。"""

    loaded_sig = pyqtSignal(int, list)  # (work_module_id, sub_modules)
    error_sig = pyqtSignal(str)

    def __init__(self, config, work_module_id, parent=None):
        super().__init__(parent)
        self.config = config
        self.work_module_id = work_module_id

    def run(self):
        try:
            session = PMSession(
                self.config.get('pm_url', ''),
                self.config.get('pm_username', ''),
                self.config.get('pm_password', ''))
            session.login()
            subs = fetch_work_sub_modules_by_module(session, self.work_module_id)
            self.loaded_sig.emit(self.work_module_id, subs)
        except Exception as e:
            logger.exception("加载子模块失败: module_id=%s", self.work_module_id)
            self.error_sig.emit(str(e))


class WorkloadSubmitWorker(QThread):
    """提交工时到 PM 系统。"""

    success_sig = pyqtSignal(str)
    error_sig = pyqtSignal(str)

    def __init__(self, config, payload, parent=None):
        super().__init__(parent)
        self.config = config
        self.payload = payload

    def run(self):
        try:
            pm_url = self.config.get('pm_url', '')
            pm_user = self.config.get('pm_username', '')
            pm_pass = self.config.get('pm_password', '')

            session = PMSession(pm_url, pm_user, pm_pass)
            session.login()

            result = submit_workload(session, self.payload)
            msg = result.get('msg', '操作成功')
            logger.info("工时提交成功: %s", msg)
            self.success_sig.emit(msg)
        except Exception as e:
            logger.exception("工时提交失败")
            self.error_sig.emit(str(e))
