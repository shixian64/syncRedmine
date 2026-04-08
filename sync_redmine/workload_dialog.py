# -*- coding: utf-8 -*-
"""每日工时提交对话框 (WorkloadDialog)。"""

from datetime import datetime, date

from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QFrame, QComboBox, QPlainTextEdit, QWidget,
    QMessageBox, QRadioButton, QButtonGroup, QDateEdit,
)
from PyQt5.QtCore import Qt, QTimer, QDate, QPropertyAnimation, QEasingCurve

from .constants import WORKLOAD_HOUR, WORKLOAD_MINUTE, logger
from .config import save_config
from .workload_workers import (
    WorkloadDropdownLoader, RedmineActivityLoader,
    SubModuleLoader, WorkloadSubmitWorker,
)
from .ui_base import (
    apply_shadow, make_badge, tint_badge, make_divider,
    GradientPanel, AnimatedDialog, SmoothScrollArea, make_icon,
)

# 业务部门选项（与 PM 系统硬编码一致）
BUSINESS_DEPARTMENTS = [
    ('1', 'ALL'), ('2', 'EDM-整机'), ('3', 'EDM-PCBA'),
    ('4', 'MT(移动终端事业部)'), ('5', 'NBD'), ('6', 'MBF-mobiwire'),
    ('7', 'MBF-Mobi IoT'), ('8', 'MBF-Sagetel hk'), ('9', 'MBF-doro'),
    ('10', '麦度'), ('11', '萨瑞'), ('12', 'Rotor'),
    ('13', '法国'), ('14', '移动终端'), ('15', '传音'),
    ('16', '业务拓展'), ('20', '麦博'),
]


class WorkloadDialog(AnimatedDialog):
    """每日工时提交对话框。"""

    STATUS_STYLES = {
        'idle': {
            'badge': ('待提交', '#dbeafe', '#1d4ed8'),
            'text_color': '#1d4ed8',
        },
        'running': {
            'badge': ('提交中', '#dbeafe', '#1d4ed8'),
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

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = dict(config) if config else {}
        self.config_changed = False
        self._dropdown_loader = None
        self._activity_loader = None
        self._submit_worker = None
        self._error_anim = None
        self._activities = []
        self._pm_user_id = None
        self._sub_module_loader = None
        self._common_projects = []
        self._setup_mode = not self._has_pm_config()

        self.setWindowTitle("syncRedmine — 每日工时提交")
        self.setWindowIcon(make_icon('#2563eb'))
        self.setFixedWidth(920)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self._build()

    def _has_pm_config(self):
        return bool(self.config.get('pm_url')
                     and self.config.get('pm_username')
                     and self.config.get('pm_password'))

    def showEvent(self, event):
        super().showEvent(event)
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            self.setMaximumHeight(screen.availableGeometry().height() - 60)
        if self._setup_mode:
            self._show_setup_panel()
        else:
            QTimer.singleShot(0, self._start_loading)

    # ═══════════════════════════════════════════════════════════════════════════
    # UI 辅助
    # ═══════════════════════════════════════════════════════════════════════════
    @staticmethod
    def _panel(title, subtitle, badge=None, badge_style=None):
        panel = QFrame()
        panel.setObjectName("SectionPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)
        head = QHBoxLayout()
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        ttl = QLabel(title)
        ttl.setObjectName("SectionTitle")
        desc = QLabel(subtitle)
        desc.setObjectName("SectionDesc")
        desc.setWordWrap(True)
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
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(4)
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
    def _mkbtn(text, role, slot):
        b = QPushButton(text)
        b.setObjectName(role)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def _le(self, placeholder='', val=''):
        e = QLineEdit(val)
        e.setPlaceholderText(placeholder)
        e.setFixedHeight(44)
        e.setClearButtonEnabled(True)
        return e

    def _le_pw(self, placeholder='', val=''):
        e = QLineEdit(val)
        e.setPlaceholderText(placeholder)
        e.setFixedHeight(44)
        e.setEchoMode(QLineEdit.Password)
        return e

    # ═══════════════════════════════════════════════════════════════════════════
    # 构建主界面
    # ═══════════════════════════════════════════════════════════════════════════
    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(0)
        root.setContentsMargins(14, 14, 14, 14)

        shell = QFrame()
        shell.setObjectName("DialogShell")
        apply_shadow(shell, blur=52, y_offset=16, alpha=34)
        root.addWidget(shell)

        body = QVBoxLayout(shell)
        body.setSpacing(12)
        body.setContentsMargins(20, 20, 20, 20)

        # ── Hero ──────────────────────────────────────────────────────
        hero = GradientPanel('#0c1a3d', '#1e40af', '#60a5fa')
        hero.setMinimumHeight(0)
        hero.setMaximumHeight(120)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setSpacing(6)
        hero_layout.setContentsMargins(20, 14, 20, 14)

        hero_top = QHBoxLayout()
        hero_top.addWidget(make_badge("工时助手", 'rgba(255,255,255,0.16)', '#ffffff'))
        hero_top.addStretch()
        today_str = datetime.now().strftime('%Y-%m-%d')
        hero_top.addWidget(make_badge(today_str, 'rgba(255,255,255,0.12)', '#bfdbfe'))
        hero_layout.addLayout(hero_top)

        title = QLabel("每日工时提交")
        title.setObjectName("HeroTitle")
        hero_layout.addWidget(title)
        subtitle = QLabel("基于今日 Redmine 活动自动生成工时内容，确认后提交到工时系统。")
        subtitle.setObjectName("HeroText")
        subtitle.setWordWrap(True)
        hero_layout.addWidget(subtitle)
        body.addWidget(hero)

        # ── 滚动区域 ──────────────────────────────────────────────────
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet("""
            QScrollArea, QScrollArea > QWidget > QWidget {
                background: transparent; border: none;
            }
            QScrollBar:vertical {
                background: transparent; width: 10px; margin: 2px 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(148, 163, 184, 0.5);
                border-radius: 5px; min-height: 40px;
            }
            QScrollBar::handle:vertical:hover { background: rgba(100, 116, 139, 0.7); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px; background: transparent;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
        """)

        inner = QWidget()
        self._inner_layout = QVBoxLayout(inner)
        self._inner_layout.setSpacing(12)
        self._inner_layout.setContentsMargins(0, 0, 0, 0)

        # ── 配置面板（首次使用）──────────────────────────────────────
        self._build_setup_panel()

        # ── 今日活动面板 ──────────────────────────────────────────────
        act_panel, self._act_layout = self._panel(
            "今日活动概览",
            "来自 Redmine 活动页面的今日记录。",
            badge="加载中")
        self._act_badge = act_panel.findChildren(QLabel)[-1]
        self._act_list_widget = QLabel("正在加载活动...")
        self._act_list_widget.setObjectName("SectionDesc")
        self._act_list_widget.setWordWrap(True)
        self._act_layout.addWidget(self._act_list_widget)
        self._act_panel = act_panel
        self._inner_layout.addWidget(act_panel)

        # ── 工时表单面板（按 PM 系统实际表单结构）─────────────────────
        self._build_form_panel()

        # ── 提交反馈条 ──────────────────────────────────────────────
        fb_strip = QFrame()
        fb_strip.setObjectName("InfoStrip")
        fb_layout = QHBoxLayout(fb_strip)
        fb_layout.setContentsMargins(14, 10, 14, 10)
        fb_layout.setSpacing(10)
        self.status_pill = make_badge("待提交", '#dbeafe', '#1d4ed8')
        fb_layout.addWidget(self.status_pill, 0, Qt.AlignVCenter)

        fb_right = QVBoxLayout()
        fb_right.setSpacing(4)
        self.status_lbl = QLabel("请确认表单内容后提交。")
        self.status_lbl.setObjectName("StatusText")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        fb_right.addWidget(self.status_lbl)

        self.error_detail = QPlainTextEdit()
        self.error_detail.setObjectName("ErrorDetail")
        self.error_detail.setReadOnly(True)
        self.error_detail.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.error_detail.setMaximumHeight(0)
        self.error_detail.hide()
        fb_right.addWidget(self.error_detail)
        fb_layout.addLayout(fb_right, 1)
        self._inner_layout.addWidget(fb_strip)

        self._inner_layout.addStretch()
        scroll.setWidget(inner)
        body.addWidget(scroll, 1)

        # ── 底部按钮 ──────────────────────────────────────────────────
        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch()
        self.btn_cancel = self._mkbtn("取消", "SecondaryButton", self.reject)
        self.btn_submit = self._mkbtn("提交", "PrimaryButton", self._submit)
        actions.addWidget(self.btn_cancel)
        actions.addWidget(self.btn_submit)
        body.addLayout(actions)

        if self._setup_mode:
            self._act_panel.hide()
            self._form_panel.hide()

    # ── 配置面板 ──────────────────────────────────────────────────────
    def _build_setup_panel(self):
        self._setup_panel = QFrame()
        self._setup_panel.setObjectName("SectionPanel")
        self._setup_panel.hide()
        sp_layout = QVBoxLayout(self._setup_panel)
        sp_layout.setContentsMargins(14, 14, 14, 14)
        sp_layout.setSpacing(8)

        sp_head = QHBoxLayout()
        sp_text = QVBoxLayout()
        sp_text.setSpacing(2)
        sp_ttl = QLabel("工时系统配置")
        sp_ttl.setObjectName("SectionTitle")
        sp_desc = QLabel("首次使用，请填写 PM 工时系统账号和 Redmine 用户 ID。")
        sp_desc.setObjectName("SectionDesc")
        sp_desc.setWordWrap(True)
        sp_text.addWidget(sp_ttl)
        sp_text.addWidget(sp_desc)
        sp_head.addLayout(sp_text, 1)
        sp_head.addWidget(make_badge("配置"))
        sp_layout.addLayout(sp_head)
        sp_layout.addWidget(make_divider())

        self.setup_pm_url = self._le('http://122.227.250.174:4333',
                                      self.config.get('pm_url', 'http://122.227.250.174:4333'))
        sp_layout.addWidget(self._field_block("PM 系统地址", self.setup_pm_url))

        row1 = QHBoxLayout()
        row1.setSpacing(12)
        self.setup_pm_user = self._le('PM 用户名（工号）', self.config.get('pm_username', ''))
        row1.addWidget(self._field_block("PM 用户名", self.setup_pm_user), 1)
        self.setup_pm_pass = self._le_pw('PM 密码', self.config.get('pm_password', ''))
        row1.addWidget(self._field_block("PM 密码", self.setup_pm_pass), 1)
        sp_layout.addLayout(row1)

        self.setup_redmine_uid = self._le('Redmine 用户 ID（数字）',
                                           str(self.config.get('redmine_user_id', '')))
        sp_layout.addWidget(self._field_block(
            "Redmine 用户 ID", self.setup_redmine_uid,
            "在 Redmine 个人页面 URL 中可找到，如 /users/1933 中的 1933"))

        row_time = QHBoxLayout()
        row_time.setSpacing(12)
        self.setup_wl_hour = self._le('17', str(self.config.get('workload_hour', WORKLOAD_HOUR)))
        row_time.addWidget(self._field_block("提醒时（0-23）", self.setup_wl_hour), 1)
        self.setup_wl_minute = self._le('0', str(self.config.get('workload_minute', WORKLOAD_MINUTE)))
        row_time.addWidget(self._field_block("提醒分（0-59）", self.setup_wl_minute), 1)
        sp_layout.addLayout(row_time)

        btn_save_setup = self._mkbtn("保存配置并继续", "PrimaryButton", self._save_setup)
        sp_layout.addWidget(btn_save_setup, alignment=Qt.AlignRight)
        self._inner_layout.addWidget(self._setup_panel)

    # ── 工时表单面板（匹配 PM 系统表单结构）──────────────────────────
    def _build_form_panel(self):
        form_panel, form_layout = self._panel(
            "工时表单",
            "字段与工时系统一致，下拉选项自动加载。",
            badge="填写")

        defaults = self.config.get('workload_defaults') or {}

        # 任务类别（radio: 预研/开发项目/common）— 必须先选择才能填写其他字段
        type_row = QHBoxLayout()
        type_row.setSpacing(16)
        self._type_group = QButtonGroup(self)
        self._type_group.setExclusive(True)
        self._radio_preresearch = QRadioButton("预研")
        self._radio_develop = QRadioButton("开发项目")
        self._radio_common = QRadioButton("common")
        self._type_group.addButton(self._radio_preresearch, 0)
        self._type_group.addButton(self._radio_develop, 1)
        self._type_group.addButton(self._radio_common, 2)
        # 有默认值时自动选中，否则不选（强制用户先选择）
        default_type = defaults.get('workloadType')
        if default_type == '0':
            self._radio_preresearch.setChecked(True)
        elif default_type == '2':
            self._radio_common.setChecked(True)
        elif default_type == '1':
            self._radio_develop.setChecked(True)
        # 无默认值时不选中任何 radio
        type_row.addWidget(self._radio_preresearch)
        type_row.addWidget(self._radio_develop)
        type_row.addWidget(self._radio_common)
        type_row.addStretch()
        type_wrapper = QFrame()
        tw_layout = QVBoxLayout(type_wrapper)
        tw_layout.setContentsMargins(0, 0, 0, 0)
        tw_layout.setSpacing(0)
        tw_layout.addLayout(type_row)
        form_layout.addWidget(self._field_block("任务类别 *", type_wrapper,
                                                 "请先选择任务类别，后续字段才可填写"))
        self._type_group.buttonClicked.connect(self._on_type_changed)

        # 开发项目（仅 type=1 时显示）— 选择后自动联动业务部门和产品形态
        self.combo_project = QComboBox()
        self.combo_project.setFixedHeight(44)
        self.combo_project.setEditable(True)
        self.combo_project.setInsertPolicy(QComboBox.NoInsert)
        self.combo_project.completer().setFilterMode(Qt.MatchContains)
        self.combo_project.completer().setCompletionMode(
            self.combo_project.completer().PopupCompletion)
        self.combo_project.setEnabled(False)
        self.combo_project.addItem("加载中...", None)
        self.combo_project.currentIndexChanged.connect(self._on_project_changed)
        self._dev_projects = []  # 原始项目列表（含 businessDepartment/productForm 关联）
        self._project_block = self._field_block("开发项目", self.combo_project)
        form_layout.addWidget(self._project_block)

        # common 项目（仅 type=2 时显示）
        self.combo_common_project = QComboBox()
        self.combo_common_project.setFixedHeight(44)
        self.combo_common_project.setEnabled(False)
        self.combo_common_project.addItem("加载中...", None)
        self._common_project_block = self._field_block("Common 项目", self.combo_common_project)
        form_layout.addWidget(self._common_project_block)

        # 业务部门（下拉框，与 PM 系统选项一致）
        row_dept = QHBoxLayout()
        row_dept.setSpacing(12)
        self.combo_department = QComboBox()
        self.combo_department.setFixedHeight(44)
        default_dept = defaults.get('businessDepartment', '')
        dept_idx = 0
        for i, (val, label) in enumerate(BUSINESS_DEPARTMENTS):
            self.combo_department.addItem(label, val)
            if val == default_dept:
                dept_idx = i
        self.combo_department.setCurrentIndex(dept_idx)
        row_dept.addWidget(self._field_block("业务部门", self.combo_department), 1)

        # 项目阶段/NPI节点（仅 type=1 时显示）
        self.combo_npi = QComboBox()
        self.combo_npi.setFixedHeight(44)
        self.combo_npi.setEnabled(False)
        self.combo_npi.addItem("加载中...", None)
        self._npi_block = self._field_block("项目阶段", self.combo_npi)
        row_dept.addWidget(self._npi_block, 1)
        form_layout.addLayout(row_dept)

        # 产品形态 + 模块（一行两列）
        row_pm = QHBoxLayout()
        row_pm.setSpacing(12)
        self.combo_product = QComboBox()
        self.combo_product.setFixedHeight(44)
        self.combo_product.setEnabled(False)
        self.combo_product.addItem("加载中...", None)
        self._product_block = self._field_block("产品形态", self.combo_product)
        row_pm.addWidget(self._product_block, 1)

        self.combo_module = QComboBox()
        self.combo_module.setFixedHeight(44)
        self.combo_module.setEnabled(False)
        self.combo_module.addItem("加载中...", None)
        self.combo_module.currentIndexChanged.connect(self._on_module_changed)
        row_pm.addWidget(self._field_block("模块", self.combo_module), 1)
        form_layout.addLayout(row_pm)

        # 子模块（单独一行）
        self.combo_submodule = QComboBox()
        self.combo_submodule.setFixedHeight(44)
        self.combo_submodule.setEnabled(False)
        self.combo_submodule.addItem("请先选择模块", None)
        form_layout.addWidget(self._field_block("子模块", self.combo_submodule))

        # 具体工作内容（textarea）
        self.edit_content = QPlainTextEdit()
        self.edit_content.setPlaceholderText("请输入具体工作内容（将自动从今日 Redmine 活动生成）")
        self.edit_content.setMinimumHeight(90)
        self.edit_content.setMaximumHeight(200)
        form_layout.addWidget(self._field_block("具体工作内容", self.edit_content))

        # 工时 + 任务日期（一行两列）
        row_ht = QHBoxLayout()
        row_ht.setSpacing(12)

        hours_row = QHBoxLayout()
        hours_row.setSpacing(4)
        btn_dec = QPushButton("\u2212")
        btn_dec.setObjectName("StepButton")
        btn_dec.setFixedSize(36, 44)
        btn_dec.setCursor(Qt.PointingHandCursor)
        self.edit_hours = QLineEdit("7")
        self.edit_hours.setAlignment(Qt.AlignCenter)
        self.edit_hours.setFixedHeight(44)
        self.edit_hours.setMaximumWidth(64)
        btn_inc = QPushButton("+")
        btn_inc.setObjectName("StepButton")
        btn_inc.setFixedSize(36, 44)
        btn_inc.setCursor(Qt.PointingHandCursor)
        btn_dec.clicked.connect(lambda: self._step_hours(-0.5))
        btn_inc.clicked.connect(lambda: self._step_hours(0.5))
        hours_row.addWidget(btn_dec)
        hours_row.addWidget(self.edit_hours)
        hours_row.addWidget(btn_inc)
        hours_hint = QLabel('<span style="color:red;font-size:8pt;">请合理填写，勿超打卡时间(司外办公除外)</span>')
        hours_hint.setTextFormat(Qt.RichText)
        hours_row.addWidget(hours_hint)
        hours_row.addStretch()
        hours_widget = QFrame()
        hw_layout = QVBoxLayout(hours_widget)
        hw_layout.setContentsMargins(0, 0, 0, 0)
        hw_layout.setSpacing(0)
        hw_layout.addLayout(hours_row)
        row_ht.addWidget(self._field_block("工时(小时)", hours_widget), 1)

        self.date_edit = QDateEdit()
        self.date_edit.setFixedHeight(44)
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setReadOnly(True)
        self.date_edit.setButtonSymbols(QDateEdit.NoButtons)
        self.date_edit.setStyleSheet(
            "QDateEdit { background: #f1f5f9; color: #64748b; }")
        row_ht.addWidget(self._field_block("任务日期（默认当天）", self.date_edit), 1)
        form_layout.addLayout(row_ht)

        # 备注 + 检查人（一行两列）
        row_ri = QHBoxLayout()
        row_ri.setSpacing(12)
        self.edit_remark = self._le('备注（可选）')
        row_ri.addWidget(self._field_block("备注", self.edit_remark), 1)

        self.combo_inspector = QComboBox()
        self.combo_inspector.setFixedHeight(44)
        self.combo_inspector.setEnabled(False)
        self.combo_inspector.addItem("加载中...", None)
        row_ri.addWidget(self._field_block("检查人", self.combo_inspector), 1)
        form_layout.addLayout(row_ri)

        self._form_panel = form_panel
        self._inner_layout.addWidget(form_panel)

        # 收集所有表单字段，用于初始禁用/启用控制
        self._form_fields = [
            self.combo_project, self.combo_common_project,
            self.combo_department, self.combo_npi,
            self.combo_product, self.combo_module, self.combo_submodule,
            self.edit_content, self.edit_hours, self.edit_remark,
            self.combo_inspector,
        ]

        # 初始化：有默认任务类别时正常显示，否则禁用所有字段
        if self._type_group.checkedId() >= 0:
            self._on_type_changed()
        else:
            self._set_form_enabled(False)
            self._project_block.hide()
            self._common_project_block.hide()
            self._npi_block.hide()
            self._product_block.hide()

    def _set_form_enabled(self, enabled):
        """整体启用/禁用表单字段。"""
        for w in self._form_fields:
            w.setEnabled(enabled)

    def _on_type_changed(self, btn=None):
        """任务类别切换时控制字段可见性和锁定状态。"""
        type_id = self._get_workload_type()
        is_develop = (type_id == '1')
        is_common = (type_id == '2')

        # 启用所有字段
        self._set_form_enabled(True)

        # 开发项目相关字段的可见性
        self._project_block.setVisible(is_develop)
        self._common_project_block.setVisible(is_common)
        self._npi_block.setVisible(is_develop)
        self._product_block.setVisible(is_develop)

        # 开发项目模式：业务部门和产品形态由项目决定，禁止手动修改
        if is_develop:
            self.combo_department.setEnabled(False)
            self.combo_product.setEnabled(False)

        # 模块/子模块/NPI/检查人要等异步加载完才启用
        if self.combo_module.count() == 1 and self.combo_module.itemData(0) is None:
            self.combo_module.setEnabled(False)
        if self.combo_submodule.count() == 1 and self.combo_submodule.itemData(0) is None:
            self.combo_submodule.setEnabled(False)
        if self.combo_npi.count() == 1 and self.combo_npi.itemData(0) is None:
            self.combo_npi.setEnabled(False)
        if self.combo_inspector.count() == 1 and self.combo_inspector.itemData(0) is None:
            self.combo_inspector.setEnabled(False)
        if self.combo_common_project.count() == 1 and self.combo_common_project.itemData(0) is None:
            self.combo_common_project.setEnabled(False)

    def _get_workload_type(self):
        checked = self._type_group.checkedId()
        return str(checked) if checked >= 0 else '1'

    # ═══════════════════════════════════════════════════════════════════════════
    # 配置面板逻辑
    # ═══════════════════════════════════════════════════════════════════════════
    def _show_setup_panel(self):
        self._setup_panel.show()
        self._act_panel.hide()
        self._form_panel.hide()
        self.btn_submit.setEnabled(False)

    def _save_setup(self):
        pm_url = self.setup_pm_url.text().strip()
        pm_user = self.setup_pm_user.text().strip()
        pm_pass = self.setup_pm_pass.text().strip()
        redmine_uid = self.setup_redmine_uid.text().strip()

        if not pm_url or not pm_user or not pm_pass:
            QMessageBox.warning(self, "提示", "请填写完整的 PM 系统地址、用户名和密码。")
            return
        if not redmine_uid:
            QMessageBox.warning(self, "提示", "请填写 Redmine 用户 ID。")
            return

        self.config['pm_url'] = pm_url
        self.config['pm_username'] = pm_user
        self.config['pm_password'] = pm_pass
        try:
            self.config['redmine_user_id'] = int(redmine_uid)
        except ValueError:
            self.config['redmine_user_id'] = redmine_uid

        try:
            h = int(self.setup_wl_hour.text().strip() or str(WORKLOAD_HOUR))
            m = int(self.setup_wl_minute.text().strip() or str(WORKLOAD_MINUTE))
            self.config['workload_hour'] = max(0, min(23, h))
            self.config['workload_minute'] = max(0, min(59, m))
        except ValueError:
            self.config['workload_hour'] = WORKLOAD_HOUR
            self.config['workload_minute'] = WORKLOAD_MINUTE

        save_config(self.config)
        self.config_changed = True
        logger.info("工时系统配置已保存")

        self._setup_mode = False
        self._setup_panel.hide()
        self._act_panel.show()
        self._form_panel.show()
        self.btn_submit.setEnabled(True)
        self._start_loading()

    # ═══════════════════════════════════════════════════════════════════════════
    # 数据加载
    # ═══════════════════════════════════════════════════════════════════════════
    def _start_loading(self):
        app = QApplication.instance()

        self._activity_loader = RedmineActivityLoader(self.config, parent=app)
        self._activity_loader.loaded_sig.connect(self._on_activities_loaded)
        self._activity_loader.error_sig.connect(self._on_activities_error)
        self._activity_loader.start()

        if self._has_pm_config():
            self._dropdown_loader = WorkloadDropdownLoader(self.config, parent=app)
            self._dropdown_loader.loaded_sig.connect(self._on_dropdowns_loaded)
            self._dropdown_loader.error_sig.connect(self._on_dropdowns_error)
            self._dropdown_loader.start()

    def _on_activities_loaded(self, activities):
        self._activities = activities
        if not activities:
            self._act_list_widget.setText("今日暂无 Redmine 活动记录。")
            tint_badge(self._act_badge, "无活动", '#fef3c7', '#92400e')
            return

        lines = []
        for act in activities:
            lines.append(
                f'<span style="color:#64748b;">{act["time"]}</span>  '
                f'<span style="color:#0369a1;font-weight:600;">{act["project"]}</span>  '
                f'<span style="color:#334155;">#{act["issue_id"]}</span>  '
                f'{act["title"]}'
            )
        self._act_list_widget.setText('<br>'.join(lines))
        self._act_list_widget.setTextFormat(Qt.RichText)
        tint_badge(self._act_badge, f"{len(activities)} 条", '#dcfce7', '#15803d')

        if not self.edit_content.toPlainText().strip():
            content_lines = [act["title"] for act in activities]
            self.edit_content.setPlainText('\n'.join(content_lines))

        act_projects = list(dict.fromkeys(a['project'] for a in activities))
        current_text = self.combo_project.currentText()
        for proj in act_projects:
            if self.combo_project.findText(proj) < 0:
                self.combo_project.addItem(proj)
        if current_text:
            idx = self.combo_project.findText(current_text)
            if idx >= 0:
                self.combo_project.setCurrentIndex(idx)

    def _on_activities_error(self, msg):
        self._act_list_widget.setText(f"加载失败: {msg}")
        tint_badge(self._act_badge, "失败", '#fee2e2', '#b91c1c')

    def _on_project_changed(self, _index):
        """选择开发项目后自动联动业务部门和产品形态（与网页行为一致）。"""
        project_name = self.combo_project.currentText()
        matched = None
        for proj in self._dev_projects:
            if proj.get('projectName') == project_name:
                matched = proj
                break

        if matched:
            # 自动填充业务部门
            dept_val = str(matched.get('businessDepartment') or '')
            dept_idx = self.combo_department.findData(dept_val)
            if dept_idx >= 0:
                self.combo_department.setCurrentIndex(dept_idx)

            # 自动填充产品形态
            pf_val = matched.get('productForm') or ''
            pf_idx = self.combo_product.findText(pf_val)
            if pf_idx >= 0:
                self.combo_product.setCurrentIndex(pf_idx)

    def _on_dropdowns_loaded(self, data):
        self._pm_user_id = data.get('user_id')
        self._sub_module_map = data.get('sub_module_map') or {}
        defaults = self.config.get('workload_defaults') or {}

        # 开发项目列表（从 API 加载，含 businessDepartment/productForm 关联）
        self._dev_projects = data.get('dev_projects') or []
        self.combo_project.blockSignals(True)
        self.combo_project.clear()
        default_proj = defaults.get('projectCategory', '')
        proj_idx = -1
        for i, proj in enumerate(self._dev_projects):
            name = proj.get('projectName') or ''
            self.combo_project.addItem(name, name)
            if default_proj and name == default_proj:
                proj_idx = i
        # 补充历史记录中的项目（可能不在 API 列表中）
        for cat in (data.get('project_categories') or []):
            if self.combo_project.findText(cat) < 0:
                self.combo_project.addItem(cat)
                if default_proj and cat == default_proj:
                    proj_idx = self.combo_project.count() - 1
        if proj_idx >= 0:
            self.combo_project.setCurrentIndex(proj_idx)
        self.combo_project.setEnabled(True)
        self.combo_project.blockSignals(False)
        # 触发一次联动
        if proj_idx >= 0:
            self._on_project_changed(proj_idx)

        # common 项目列表
        self._common_projects = data.get('common_projects') or []
        self.combo_common_project.blockSignals(True)
        self.combo_common_project.clear()
        default_common_id = defaults.get('commonProjectId')
        default_common_name = defaults.get('outerProjectCategory', '')
        common_idx = 0
        self.combo_common_project.addItem("请选择 common 项目", None)
        for i, proj in enumerate(self._common_projects):
            pid = proj.get('commonProjectId')
            name = proj.get('commonProjectName') or proj.get('commonProjectDescription') or str(pid)
            self.combo_common_project.addItem(name, pid)
            if default_common_id and str(pid) == str(default_common_id):
                common_idx = i + 1
            elif default_common_name and name == default_common_name:
                common_idx = i + 1
        self.combo_common_project.setCurrentIndex(common_idx)
        self.combo_common_project.setEnabled(bool(self._common_projects))
        self.combo_common_project.blockSignals(False)

        # 模块（与网页一致：只有1个时自动选，否则不预选）
        modules = data.get('modules') or []
        self.combo_module.blockSignals(True)
        self.combo_module.clear()
        default_mod = defaults.get('workModuleId')
        sel_idx = -1
        self.combo_module.addItem("请选择模块", None)
        for i, mod in enumerate(modules):
            mid = mod.get('workModuleId')
            name = mod.get('workModuleName') or str(mid)
            self.combo_module.addItem(name, mid)
            if default_mod and str(mid) == str(default_mod):
                sel_idx = i + 1  # +1 因为第0项是占位
        if sel_idx >= 0:
            self.combo_module.setCurrentIndex(sel_idx)
        elif len(modules) == 1:
            # 网页行为：只有1个模块时自动选中
            self.combo_module.setCurrentIndex(1)
        else:
            self.combo_module.setCurrentIndex(0)
        self.combo_module.setEnabled(True)
        self.combo_module.blockSignals(False)
        self._on_module_changed(self.combo_module.currentIndex())

        # 项目阶段 / NPI节点
        npi_nodes = data.get('npi_nodes') or []
        self.combo_npi.blockSignals(True)
        self.combo_npi.clear()
        default_npi = defaults.get('workloadNpiNode')
        npi_idx = 0
        for i, node in enumerate(npi_nodes):
            name = node.get('nodeName') or node.get('nodeDescription') or ''
            self.combo_npi.addItem(name, name)
            if default_npi and name == default_npi:
                npi_idx = i
        self.combo_npi.setCurrentIndex(npi_idx)
        self.combo_npi.setEnabled(True)
        self.combo_npi.blockSignals(False)

        # 产品形态
        forms = data.get('product_forms') or []
        self.combo_product.blockSignals(True)
        self.combo_product.clear()
        default_pf = defaults.get('productForm')
        pf_idx = 0
        for i, form in enumerate(forms):
            name = form.get('productFormName') or ''
            self.combo_product.addItem(name, name)
            if default_pf and name == default_pf:
                pf_idx = i
        self.combo_product.setCurrentIndex(pf_idx)
        self.combo_product.setEnabled(True)
        self.combo_product.blockSignals(False)

        # 检查人（与网页一致：自动选第一个）
        persons = data.get('check_persons') or []
        self.combo_inspector.blockSignals(True)
        self.combo_inspector.clear()
        default_insp = defaults.get('inspectorId')
        insp_idx = 0  # 网页行为：默认选第一个
        for i, p in enumerate(persons):
            uid = p.get('userId')
            nick = p.get('userNick') or p.get('userName') or str(uid)
            self.combo_inspector.addItem(nick, uid)
            if default_insp and str(uid) == str(default_insp):
                insp_idx = i
        self.combo_inspector.setCurrentIndex(insp_idx)
        self.combo_inspector.setEnabled(True)
        self.combo_inspector.blockSignals(False)

        logger.info("工时下拉选项加载完成: dev_projects=%d common_projects=%d modules=%d npi=%d forms=%d persons=%d",
                     len(self._dev_projects), len(self._common_projects),
                     len(modules), len(npi_nodes), len(forms), len(persons))

        # 重新应用任务类别的锁定规则
        if self._type_group.checkedId() >= 0:
            self._on_type_changed()
        else:
            self._set_form_enabled(False)

    def _on_dropdowns_error(self, msg):
        self._set_status(f"下拉选项加载失败: {msg}", state='error')
        for combo in (
            self.combo_common_project,
            self.combo_module, self.combo_npi,
            self.combo_product, self.combo_inspector,
        ):
            combo.clear()
            combo.addItem("加载失败", None)

    def _on_module_changed(self, index):
        """选择模块后异步加载子模块（与网页 API 一致）。"""
        mod_id = self.combo_module.currentData()
        self.combo_submodule.blockSignals(True)
        self.combo_submodule.clear()

        if not mod_id:
            self.combo_submodule.addItem("请先选择模块", None)
            self.combo_submodule.setEnabled(False)
            self.combo_submodule.blockSignals(False)
            return

        self.combo_submodule.addItem("加载中...", None)
        self.combo_submodule.setEnabled(False)
        self.combo_submodule.blockSignals(False)

        # 异步请求子模块
        if self._sub_module_loader and self._sub_module_loader.isRunning():
            self._sub_module_loader.disconnect()
        loader = SubModuleLoader(self.config, mod_id, parent=QApplication.instance())
        loader.loaded_sig.connect(self._on_sub_modules_loaded)
        loader.error_sig.connect(self._on_sub_modules_error)
        self._sub_module_loader = loader
        loader.start()

    def _on_sub_modules_loaded(self, module_id, subs):
        """子模块加载完成回调。"""
        # 确认当前选中的模块没变
        if self.combo_module.currentData() != module_id:
            return

        defaults = self.config.get('workload_defaults') or {}
        default_sub = defaults.get('workSubModuleId')

        self.combo_submodule.blockSignals(True)
        self.combo_submodule.clear()
        self.combo_submodule.addItem("请选择子模块", None)
        sub_idx = -1
        for i, s in enumerate(subs):
            sid = s.get('workSubModuleId')
            name = s.get('workSubModuleName') or str(sid)
            self.combo_submodule.addItem(name, sid)
            if default_sub and str(sid) == str(default_sub):
                sub_idx = i + 1
        if sub_idx >= 0:
            self.combo_submodule.setCurrentIndex(sub_idx)
        elif len(subs) == 1:
            self.combo_submodule.setCurrentIndex(1)
        else:
            self.combo_submodule.setCurrentIndex(0)
        self.combo_submodule.setEnabled(bool(subs))
        self.combo_submodule.blockSignals(False)

    def _on_sub_modules_error(self, msg):
        self.combo_submodule.blockSignals(True)
        self.combo_submodule.clear()
        self.combo_submodule.addItem("加载失败", None)
        self.combo_submodule.setEnabled(False)
        self.combo_submodule.blockSignals(False)

    # ═══════════════════════════════════════════════════════════════════════════
    # 工时操作
    # ═══════════════════════════════════════════════════════════════════════════
    def _step_hours(self, delta):
        try:
            val = float(self.edit_hours.text().strip() or '0')
        except ValueError:
            val = 7
        val = max(0.5, min(16, round(val + delta, 1)))
        self.edit_hours.setText(str(val))

    # ═══════════════════════════════════════════════════════════════════════════
    # 提交
    # ═══════════════════════════════════════════════════════════════════════════
    def _collect_payload(self):
        hours_text = self.edit_hours.text().strip()
        try:
            hours = float(hours_text) if hours_text else 7
        except ValueError:
            hours = 7

        wl_type = self._get_workload_type()
        common_project_id = self.combo_common_project.currentData()
        common_project_name = (
            self.combo_common_project.currentText().strip()
            if common_project_id is not None else ''
        )

        # 任务日期
        qdate = self.date_edit.date()
        work_time = datetime(qdate.year(), qdate.month(), qdate.day(), 9, 0, 0)

        payload = {
            'workloadType': wl_type,
            'preResearchProjectId': '',
            'commonProjectId': str(common_project_id) if wl_type == '2' and common_project_id is not None else '',
            'projectCategory': self.combo_project.currentText().strip() if wl_type == '1' else '',
            'outerProjectCategory': common_project_name if wl_type == '2' else '',
            'businessDepartment': self.combo_department.currentData() or '',
            'workloadNpiNode': (self.combo_npi.currentData() or self.combo_npi.currentText()) if wl_type == '1' else '',
            'productForm': (self.combo_product.currentData() or self.combo_product.currentText()) if wl_type == '1' else '',
            'workModuleId': str(self.combo_module.currentData() or ''),
            'workSubModuleId': str(self.combo_submodule.currentData() or ''),
            'workContent': self.edit_content.toPlainText().strip(),
            'workHour': hours,
            'workTime': work_time.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            'remark': self.edit_remark.text().strip(),
            'inspectorId': str(self.combo_inspector.currentData() or ''),
            'creatorId': self._pm_user_id or self.config.get('pm_user_id', ''),
            'checkStatus': '',
            'checkTime': '',
            'checkFeedback': '',
        }
        return payload

    def _submit(self):
        if self._type_group.checkedId() < 0:
            QMessageBox.warning(self, "提示", "请先选择任务类别。")
            return
        wl_type = self._get_workload_type()
        if wl_type == '1' and not self.combo_project.currentText().strip():
            QMessageBox.warning(self, "提示", "请选择开发项目。")
            return
        if wl_type == '2' and self.combo_common_project.currentData() is None:
            QMessageBox.warning(self, "提示", "请选择 common 项目。")
            return
        content = self.edit_content.toPlainText().strip()
        if not content:
            QMessageBox.warning(self, "提示", "具体工作内容不能为空。")
            return

        self.btn_submit.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.error_detail.clear()
        self._toggle_error_detail(False)
        self._set_status("提交中...", state='running')

        payload = self._collect_payload()
        project_name = (
            payload.get('projectCategory')
            or payload.get('outerProjectCategory')
            or payload.get('commonProjectId')
        )
        logger.info("提交工时: type=%s project=%s module=%s hours=%s",
                     payload.get('workloadType'), project_name,
                     payload.get('workModuleId'), payload.get('workHour'))

        self._submit_worker = WorkloadSubmitWorker(
            self.config, payload, parent=QApplication.instance())
        self._submit_worker.success_sig.connect(self._on_submit_success)
        self._submit_worker.error_sig.connect(self._on_submit_error)
        self._submit_worker.start()

    def _on_submit_success(self, msg):
        self._set_status(f"\u2713  {msg}\n窗口即将自动关闭。", state='success')
        self.btn_submit.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        logger.info("工时提交成功")

        self.config['workload_defaults'] = {
            'workloadType': self._get_workload_type(),
            'projectCategory': self.combo_project.currentText().strip(),
            'commonProjectId': str(self.combo_common_project.currentData() or ''),
            'outerProjectCategory': (
                self.combo_common_project.currentText().strip()
                if self.combo_common_project.currentData() is not None else ''
            ),
            'businessDepartment': self.combo_department.currentData() or '',
            'workModuleId': str(self.combo_module.currentData() or ''),
            'workSubModuleId': str(self.combo_submodule.currentData() or ''),
            'workloadNpiNode': self.combo_npi.currentData() or self.combo_npi.currentText(),
            'productForm': self.combo_product.currentData() or self.combo_product.currentText(),
            'inspectorId': str(self.combo_inspector.currentData() or ''),
        }
        save_config(self.config)
        self.config_changed = True
        QTimer.singleShot(800, self.accept)

    def _on_submit_error(self, msg):
        self.error_detail.setPlainText(msg)
        self._toggle_error_detail(True)
        self._set_status("\u2717  提交失败，详细错误见下方", state='error')
        self.btn_submit.setText("重试")
        self.btn_submit.setEnabled(True)
        self.btn_cancel.setEnabled(True)

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
