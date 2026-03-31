# syncRedmine

提交代码后，自动将 commit 信息同步到 Redmine 对应问题。

## 工作原理

```
commit 工具写入 ~/commit_data.log
        │
        ▼
inotify 检测到文件变化 ──→ 记录 Gerrit 基线
                                │
                                ▼
                  轮询 Gerrit API（Bug number / Topic ID）
                                │
                                ▼
                  检测到新 change / patchset / 最近更新
                                │
                                ▼
                   弹窗确认 ──→ 同步到 Redmine
```

**核心特性**

- **零侵入** — 不修改 commit 工具的任何代码
- **低开销** — 基于 inotify 内核事件监控，空闲时 CPU 零占用
- **智能检测** — 优先按 Bug number 关联，兼容 Topic ID；通过 change 快照对比 + 最近更新时间兜底
- **自动日志** — 写入 `~/.local/share/syncRedmine/logs/`，按天滚动，仅保留最近 3 天

## 同步字段

| 来源 | Redmine 字段 | 说明 |
|---|---|---|
| Bug number | issue ID | 定位 Redmine 问题 |
| Topic ID | *(不写入)* | Gerrit 检测补充条件 |
| Gerrit API | 【修复情况】 | Gerrit change URL |
| Root Cause | 【问题根源】 | — |
| Solution | 【修复方案】 | — |
| Test_Report | 【自测情况】 | — |
| Test_Suggestion | 【建议】 | — |
| Comment | 【查找问题的思路】 | 为空时填写"请填写"，同步前可手动修改 |
| 同步弹窗输入 | 工时 | 优先写入"工时"自定义字段，否则写入 `time_entries` |
| Redmine 登录用户 | 解决者 | 优先按字段名查找；无自定义字段时回退到 `assigned_to_id` |
| 同步弹窗选择 | 状态 | `OnGoing` / `Fixed` |
| 固定值 | 完成度 | 100% |

## 安装

### 环境要求

- Ubuntu 桌面系统（需要系统托盘）
- Python 3.6+
- 依赖：PyQt5、requests、paramiko

### 安装步骤

```bash
cd syncRedmine/
chmod +x install.sh
./install.sh
```

安装脚本会自动完成以下操作：

1. 检查并升级用户侧 `pip`
2. 安装 Python 依赖（优先使用二进制 wheel）
3. 复制程序到 `~/.local/share/syncRedmine/`
4. 创建开机自启动配置
5. 询问是否立即启动

> **提示**：如果 `pip` 版本过旧，`PyQt5` 可能退回源码构建导致失败。安装脚本会先尝试升级 `pip` 以避免此问题。

### 首次启动

首次启动会弹出设置窗口，需填写：

- **Gerrit** — 服务器地址、用户名、密码（REST API 检测 push）
- **Redmine** — 服务器地址、用户名、密码（同步问题字段）
- **自动更新** — 默认开启，每天 10:00 通过 SSH 从源机器同步整个仓库快照，发现新版本后自动执行 `install.sh` 覆盖安装并重启。设置页也支持手动"同步代码"

配置保存在 `~/.commit_tool/sync_config.json`（密码 base64 编码）。

## 使用

### 日常流程

安装后无需额外操作，程序开机自动启动并常驻系统托盘：

1. 正常使用 commit 工具提交代码
2. push 后自动弹出确认框
3. 确认或补充 **查找问题的思路 / 工时 / 状态**
4. 点击 **「立即同步」**

### 托盘图标

| 颜色 | 含义 |
|---|---|
| 蓝色 | 空闲，监听中 |
| 橙色 | 检测到新提交，等待 push |
| 绿色（感叹号） | push 完成，等待确认同步 |

### 右键菜单

- **设置** — 修改 Gerrit / Redmine 账号及自动更新配置
- **退出** — 关闭程序

### 命令行

```bash
# 启动（常驻托盘）
python3 ~/.local/share/syncRedmine/syncRedmine.py &

# 仅打开设置
python3 ~/.local/share/syncRedmine/syncRedmine.py --setup
```

### 日志

| 项目 | 说明 |
|---|---|
| 目录 | `~/.local/share/syncRedmine/logs/` |
| 当前日志 | `syncRedmine.log` |
| 滚动策略 | 按天切分，保留 3 天 |

## 卸载

```bash
cd syncRedmine/
chmod +x uninstall.sh
./uninstall.sh
```

卸载脚本会停止进程、删除程序文件和自启动配置，并询问是否删除账号配置。

## 项目结构

```
syncRedmine/
├── syncRedmine.py          # 入口
├── sync_redmine/           # 核心包
│   ├── __init__.py
│   ├── app.py              # 应用主逻辑（托盘、事件循环）
│   ├── api.py              # Gerrit / Redmine API 封装
│   ├── config.py           # 配置读写
│   ├── constants.py        # 常量定义
│   ├── dialogs.py          # 弹窗 UI（确认同步、设置）
│   ├── ui_base.py          # UI 基础组件
│   └── workers.py          # 后台线程（inotify、轮询）
├── install.sh              # 安装脚本
├── uninstall.sh            # 卸载脚本
├── requirements.txt        # Python 依赖
└── README.md
```

### 运行时文件

| 路径 | 说明 |
|---|---|
| `~/.local/share/syncRedmine/` | 安装后的程序目录 |
| `~/.config/autostart/syncRedmine.desktop` | 开机自启动配置 |
| `~/.commit_tool/sync_config.json` | 账号配置（密码 base64 编码） |
| `~/commit_data.log` | commit 工具写入的提交日志（只读监控） |
| `~/.local/share/syncRedmine/logs/` | 运行日志（按天滚动，保留 3 天） |
