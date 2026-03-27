# syncRedmine

提交代码后，自动将 commit 信息同步到 Redmine 对应问题。

## 工作原理

```
commit 工具 ──→ 写入 ~/commit_data.log ──→ git push
                       ↑                         ↑
              inotify 检测到文件变化  用 Bug number / Topic ID 轮询 Gerrit API
                       ↓                         ↓
                 记录 Gerrit 基线  ──────→  检测新 change / patchset / 最近更新时间
                                                  ↓
                                    用户点"是" → 同步到 Redmine
```

- **零侵入**：不修改 commit 工具的任何代码
- **inotify**：利用 Linux 内核事件监控文件变化，空闲时 CPU 零占用
- **自动检测 push**：优先按 `Bug number` 关联，同时兼容 `Topic ID`；通过 change 快照对比，并对“基线建立前 push 已完成”场景做最近更新时间兜底
- **运行日志**：自动写入 `~/.local/share/syncRedmine/logs/`，按天滚动，只保留最近 3 天日志，避免长期积累

## 同步的字段

| commit_data.log 字段 | Redmine 字段 | 说明 |
|---|---|---|
| Bug number | issue ID | 定位 Redmine 问题 |
| Topic ID | *(不写入 Redmine)* | 作为 Gerrit 检测补充条件 |
| *(Gerrit API 获取)* | 【修复情况】 | Gerrit change URL |
| Root Cause | 【问题根源】 | |
| Solution | 【修复方案】 | |
| Test_Report | 【自测情况】 | |
| Test_Suggestion | 【建议】 | |
| Comment | 【查找问题的思路】 | 如为空则填写"请填写"，同步前可手动修改 |
| *(同步弹窗输入)* | 工时 | 优先写入名称包含“工时”的自定义字段，否则写入 `time_entries` |
| *(Redmine 登录用户)* | 解决者 | 优先按字段名动态查找；无自定义字段时回退到 `assigned_to_id` |
| *(同步弹窗选择)* | 状态 | 支持 `OnGoing` / `Fixed` |
| *(固定值)* | 完成度 → 100% | |

## 安装

### 环境要求

- Ubuntu 桌面系统（需要系统托盘支持）
- Python 3.6+
- PyQt5、requests、paramiko

### 安装步骤

```bash
cd syncRedmine/
chmod +x install.sh
./install.sh
```

安装脚本会：
1. 自动检查 `pip` 版本，必要时先升级用户侧 `pip`
2. 安装 Python 依赖（`requests`、`PyQt5`、`paramiko`，优先使用二进制 wheel）
3. 复制程序到 `~/.local/share/syncRedmine/`
4. 创建开机自启动配置（`~/.config/autostart/syncRedmine.desktop`）
5. 询问是否立即启动

如果机器上的 `pip` 版本过旧，安装 `PyQt5` 时可能会退回源码构建并失败；当前安装脚本会先尝试升级用户侧 `pip`，降低这类问题出现的概率。

### 首次启动

首次启动会弹出设置窗口，需填写：

- **Gerrit**：服务器地址、用户名、密码（用于 REST API 检测 push）
- **Redmine**：服务器地址、用户名、密码（用于同步问题字段）
- **自动更新**：默认开启，可配置源机器 IP、SSH 端口、SSH 用户名/密码、远端 `syncRedmine.py` 路径；程序会每天 **10:00** 自动检查并在发现新版本后重启

配置保存在 `~/.commit_tool/sync_config.json`（密码 base64 编码）。

## 使用

### 日常使用

安装后无需任何操作，程序开机自动启动并常驻系统托盘：

1. 正常使用 commit 工具提交代码
2. push 完成后 syncRedmine 自动弹出确认框
3. 在弹窗中确认或补充 **查找问题的思路 / 工时 / 状态**
4. 点击 **「立即同步」**
5. 同步结果即时显示

### 托盘图标状态

| 图标颜色 | 含义 |
|---|---|
| 蓝色 | 空闲，监听中 |
| 橙色 | 检测到新提交，等待 push 完成 |
| 绿色（带感叹号） | push 完成，等待用户确认同步 |

### 右键菜单

- **设置**：修改 Gerrit / Redmine 账号，以及自动更新配置
- **退出**：关闭程序

### 命令行

```bash
# 启动程序（常驻托盘）
python3 ~/.local/share/syncRedmine/syncRedmine.py &

# 仅打开设置
python3 ~/.local/share/syncRedmine/syncRedmine.py --setup
```

### 运行日志

- 日志目录：`~/.local/share/syncRedmine/logs/`
- 当前日志：`~/.local/share/syncRedmine/logs/syncRedmine.log`
- 滚动策略：按天切分，保留当天日志 + 前 2 天日志
- 清理策略：进入第 4 天时，自动删除第 1 天的旧日志

## 卸载

```bash
cd syncRedmine/
chmod +x uninstall.sh
./uninstall.sh
```

卸载脚本会：
1. 停止运行中的 syncRedmine 进程
2. 删除程序文件和自启动配置
3. 询问是否删除账号配置

## 文件说明

```
syncRedmine/
├── .gitignore         # Git 忽略规则
├── syncRedmine.py     # 主程序
├── requirements.txt   # Python 依赖
├── install.sh         # 安装脚本
├── uninstall.sh       # 卸载脚本
└── README.md          # 本文档
```

### 运行时文件

| 路径 | 说明 |
|---|---|
| `~/.local/share/syncRedmine/syncRedmine.py` | 安装后的程序 |
| `~/.config/autostart/syncRedmine.desktop` | 开机自启动配置 |
| `~/.commit_tool/sync_config.json` | 设置配置（密码 base64 编码） |
| `~/commit_data.log` | commit 工具写入的提交日志（只读监控） |
| `~/.local/share/syncRedmine/logs/syncRedmine.log` | 运行日志（按天滚动，保留 3 天） |
