# -*- coding: utf-8 -*-
"""设置对话框 (SetupDialog) 与同步确认对话框 (SyncDialog)。"""

import sys, os, subprocess

from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QMessageBox,
    QFrame, QComboBox, QPlainTextEdit, QCheckBox, QWidget,
)
from PyQt5.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve

from .constants import PLACEHOLDER, GITHUB_DEFAULT_REPO, GITHUB_DEFAULT_BRANCH, logger
from .config import save_config
from .workers import AutoUpdateWorker, SyncWorker, SolverChoicesLoader
from .api import extract_first_number
from .ui_base import (
    apply_shadow, make_badge, tint_badge, make_divider,
    GradientPanel, AnimatedDialog, SmoothScrollArea, make_icon,
)


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
        self._manual_update_worker = None
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
        if isinstance(badge_text, QWidget):
            head.addWidget(badge_text)
        else:
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
        if hasattr(self, 'update_meta'):
            self.update_meta.setVisible(checked)
        self._refresh_update_summary()

    def _refresh_update_summary(self):
        if not hasattr(self, 'update_summary'):
            return
        if self._manual_update_worker is not None and self._manual_update_worker.isRunning():
            self.update_state.setText("正在同步代码…")
            self.update_summary.setText("正在从 GitHub 下载最新版本，请稍候。")
            return
        enabled = self.update_enabled.isChecked()
        self.update_state.setText("自动更新已启用" if enabled else "自动更新已关闭")
        if not enabled:
            self.update_summary.setText("关闭后不再按计划检查新脚本。")
            return

        repo = self.u_repo.text().strip()
        branch = self.u_branch.currentText() or GITHUB_DEFAULT_BRANCH
        local_ver = AutoUpdateWorker._read_local_version()
        ver_info = f"当前版本：{local_ver[:8]}" if local_ver else "当前版本：未知"
        if repo:
            self.update_summary.setText(f"每日 10:00 自动检查更新 · {repo} ({branch}) · {ver_info}")
        else:
            self.update_summary.setText("每日 10:00 自动检查更新 · 未配置 GitHub 仓库")

    def _collect_form_config(self):
        return {
            'gerrit_url': self.g_url.text().strip(),
            'gerrit_username': self.g_user.text().strip(),
            'gerrit_password': self.g_pass.text().strip(),
            'redmine_url': self.r_url.text().strip(),
            'redmine_username': self.r_user.text().strip(),
            'redmine_password': self.r_pass.text().strip(),
            'auto_update_enabled': self.update_enabled.isChecked(),
            'github_repo': self.u_repo.text().strip(),
            'github_branch': self.u_branch.currentText() or GITHUB_DEFAULT_BRANCH,
        }

    def _set_manual_update_button_busy(self, busy):
        if hasattr(self, 'manual_update_button'):
            self.manual_update_button.setEnabled(not busy)
            self.manual_update_button.setText("同步中..." if busy else "同步代码")
        self._refresh_update_summary()

    def _start_manual_update(self):
        if self._manual_update_worker is not None and self._manual_update_worker.isRunning():
            logger.info("设置窗口中已有手动同步任务在执行，忽略重复点击")
            return

        config = self._collect_form_config()
        logger.info("用户在设置窗口手动触发代码同步")
        worker = AutoUpdateWorker(config, os.path.abspath(sys.argv[0]), parent=QApplication.instance())
        worker.finished_sig.connect(self._on_manual_update_done)
        worker.finished.connect(lambda w=worker: self._on_manual_update_worker_finished(w))
        self._manual_update_worker = worker
        self._set_manual_update_button_busy(True)
        worker.start()

    def _on_manual_update_done(self, ok, changed, message):
        if ok and changed:
            logger.info("手动同步代码完成：%s", message)
            QMessageBox.information(self, "syncRedmine", "检测到新版本，已完成同步，程序即将重启。")
            QTimer.singleShot(150, self._restart_after_manual_update)
        elif ok:
            logger.info("手动同步代码结果：%s", message)
            QMessageBox.information(self, "syncRedmine", message)
        else:
            logger.warning("手动同步代码失败：%s", message)
            QMessageBox.warning(self, "syncRedmine", f"同步代码失败：{message}")

    def _on_manual_update_worker_finished(self, worker):
        if self._manual_update_worker is worker:
            self._manual_update_worker = None
        self._set_manual_update_button_busy(False)
        worker.deleteLater()

    def _restart_after_manual_update(self):
        script_path = os.path.abspath(sys.argv[0])
        try:
            subprocess.Popen(
                [sys.executable, script_path],
                cwd=os.path.dirname(script_path) or None,
                start_new_session=True,
            )
            logger.info("手动同步代码后已拉起新进程，准备退出当前实例: %s", script_path)
        except Exception as e:
            logger.exception("手动同步代码后重启 syncRedmine 失败")
            QMessageBox.warning(self, "syncRedmine", f"自动重启失败：{e}")
            return
        QApplication.instance().quit()

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
        scroll = SmoothScrollArea()
        self.scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet("""
            QScrollArea, QScrollArea > QWidget > QWidget {
                background: transparent;
                border: none;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 12px;
                margin: 4px 0 4px 4px;
            }
            QScrollBar::groove:vertical {
                background: rgba(203, 213, 225, 0.36);
                border-radius: 6px;
                margin: 0 1px 0 2px;
            }
            QScrollBar::handle:vertical {
                background: qlineargradient(
                    x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(125, 176, 255, 0.95),
                    stop:1 rgba(37, 99, 235, 0.88)
                );
                border-radius: 6px;
                min-height: 60px;
                margin: 0 1px 0 2px;
            }
            QScrollBar::handle:vertical:hover {
                background: qlineargradient(
                    x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(147, 197, 253, 0.98),
                    stop:1 rgba(29, 78, 216, 0.92)
                );
            }
            QScrollBar::handle:vertical:pressed {
                background: rgba(29, 78, 216, 0.96);
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
                background: transparent;
                border: none;
            }
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: transparent;
            }
        """)

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setSpacing(18)
        inner_layout.setContentsMargins(6, 0, 0, 0)

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

        hint = QLabel("用于检测 Gerrit push、同步提交说明到 Redmine，并可按计划自动同步最新代码。")
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

        self.manual_update_button = self._mkbtn("同步代码", "GhostButton", self._start_manual_update)
        u_panel, u_layout = self._panel(
            "自动更新",
            "每日 10:00 从 GitHub 检查并同步最新版本；也可点击右侧按钮手动同步代码。",
            self.manual_update_button)

        self.update_enabled = QCheckBox("启用每日 10:00 自动更新")
        self.update_enabled.setChecked(bool(cfg.get('auto_update_enabled', True)))
        u_layout.addWidget(self.update_enabled)

        self.u_repo = self._le(GITHUB_DEFAULT_REPO, val=cfg.get('github_repo', GITHUB_DEFAULT_REPO))
        self.u_repo.setReadOnly(True)
        self.u_repo.setStyleSheet("QLineEdit { background: #f1f5f9; color: #64748b; }")

        self.u_branch = QComboBox()
        self.u_branch.addItems(['main', 'dev'])
        saved_branch = cfg.get('github_branch', GITHUB_DEFAULT_BRANCH)
        idx = self.u_branch.findText(saved_branch)
        self.u_branch.setCurrentIndex(idx if idx >= 0 else 0)
        self.u_branch.setFixedHeight(44)
        self._update_widgets = [self.u_branch]

        self.update_meta = QFrame()
        self.update_meta.setObjectName("InlineInfoBlock")
        update_meta_layout = QVBoxLayout(self.update_meta)
        update_meta_layout.setContentsMargins(14, 12, 14, 12)
        update_meta_layout.setSpacing(8)

        update_meta_top = QHBoxLayout()
        update_meta_top.setContentsMargins(0, 0, 0, 0)
        update_meta_top.setSpacing(10)
        self.update_state = QLabel()
        self.update_state.setObjectName("InlineState")
        update_meta_top.addWidget(self.update_state, 0, Qt.AlignVCenter)
        update_meta_top.addStretch()
        update_meta_layout.addLayout(update_meta_top)

        self.update_summary = QLabel()
        self.update_summary.setObjectName("InlineSummary")
        self.update_summary.setWordWrap(True)
        update_meta_layout.addWidget(self.update_summary)
        u_layout.addWidget(self.update_meta)

        row_repo = QHBoxLayout()
        row_repo.setSpacing(12)
        row_repo.addWidget(self._field_block("GitHub 仓库", self.u_repo, f"格式：owner/repo，默认：{GITHUB_DEFAULT_REPO}"), 1)
        row_repo.addWidget(self._field_block("分支", self.u_branch, "选择要同步的分支"), 1)
        u_layout.addLayout(row_repo)

        self.u_branch.currentTextChanged.connect(self._refresh_update_summary)
        self.update_enabled.toggled.connect(self._toggle_auto_update_fields)
        checked = self.update_enabled.isChecked()
        for w in self._update_widgets:
            w.setEnabled(checked)
        self.update_meta.setVisible(checked)
        self._refresh_update_summary()

        inner_layout.addWidget(u_panel)

        info = QFrame()
        info.setObjectName("InfoStrip")
        info_layout = QHBoxLayout(info)
        info_layout.setContentsMargins(14, 12, 14, 12)
        info_layout.setSpacing(10)
        info_layout.addWidget(make_badge("本地", '#dbeafe', '#1d4ed8'), 0, Qt.AlignTop)

        note = QLabel(
            "<span style='font-weight:700;color:#0f172a;'>配置仅保存在本机</span>"
            " · 自动更新只替换当前脚本并重启，不改动 commit_tool。")
        note.setObjectName("MetaText")
        note.setWordWrap(True)
        info_layout.addWidget(note, 1)
        inner_layout.addWidget(info)
        inner_layout.addStretch()

        scroll.setWidget(inner)
        body.addWidget(scroll, 1)

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
        self.config = self._collect_form_config()
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
        gerrit_display = self.gerrit_url or f'未获取到，后续将填写"{PLACEHOLDER}"'
        preview_layout.addWidget(self._make_value_widget("Gerrit", gerrit_display, link=self.gerrit_url or None))
        top.addWidget(preview_panel, 1)

        edit_panel, edit_layout = self._panel(
            "同步前确认",
            "补充将写回 Redmine 的字段。",
            badge="编辑")

        self.edit_comment = QPlainTextEdit()
        self.edit_comment.setPlainText(self.fields.get('Comment', '').strip())
        self.edit_comment.setPlaceholderText('请填写查找问题的思路（留空默认"请填写"）')
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

        updates_info = QLabel('工时会优先写入名称包含"工时"的自定义字段，无匹配时回退到 time_entries。')
        updates_info.setObjectName("MetaText")
        updates_info.setWordWrap(True)
        updates_layout.addWidget(updates_info)
        bottom.addWidget(updates_panel, 1)

        feedback_panel, feedback_layout = self._panel(
            "同步反馈",
            '点击"立即同步"后在这里查看进度与异常详情。',
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
