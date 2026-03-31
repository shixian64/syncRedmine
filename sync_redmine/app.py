# -*- coding: utf-8 -*-
"""SyncRedmineApp —— 托盘常驻 + 自动检测主应用。"""

import sys, os, subprocess

from PyQt5.QtWidgets import (
    QApplication, QDialog, QMessageBox,
    QSystemTrayIcon, QMenu,
)
from PyQt5.QtCore import Qt, QTimer, QFileSystemWatcher
from datetime import datetime, timedelta, timezone

from .constants import (
    DEFAULT_LOG, AUTO_UPDATE_HOUR, AUTO_UPDATE_MINUTE,
    logger,
)
from .config import load_config
from .api import parse_commit_log, extract_first_number, get_gerrit_topics
from .workers import GerritPoller, AutoUpdateWorker
from .ui_base import make_icon
from .dialogs import SetupDialog, SyncDialog


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
        menu.addAction("退出", app.quit)
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
