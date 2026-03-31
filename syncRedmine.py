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

import sys

try:
    import requests
except ImportError:
    print("[syncRedmine] 缺少依赖: pip3 install requests")
    sys.exit(1)

from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMessageBox

from sync_redmine.constants import APP_STYLE_SHEET
from sync_redmine.config import load_config
from sync_redmine.ui_base import make_icon
from sync_redmine.dialogs import SetupDialog
from sync_redmine.app import SyncRedmineApp


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
