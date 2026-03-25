# syncRedmine

提交代码后，自动将 commit 信息同步到 Redmine 对应问题。

## 工作原理

```
commit 工具 ──→ 写入 ~/commit_data.log ──→ git push
                       ↑                         ↑
              inotify 检测到文件变化      轮询 Gerrit API 检测到新 change
                       ↓                         ↓
                 记录 Gerrit 快照  ──────→  弹出确认对话框
                                                  ↓
                                    用户点"是" → 同步到 Redmine
```

- **零侵入**：不修改 commit 工具的任何代码
- **inotify**：利用 Linux 内核事件监控文件变化，空闲时 CPU 零占用
- **自动检测 push**：通过 Gerrit REST API 对比 change 快照，发现新提交或 patchset 更新即判定 push 完成

## 同步的字段

| commit_data.log 字段 | Redmine 字段 | 说明 |
|---|---|---|
| Bug number | issue ID | 定位 Redmine 问题 |
| *(Gerrit API 获取)* | 【修复情况】 | Gerrit change URL |
| Root Cause | 【问题根源】 | |
| Solution | 【修复方案】 | |
| Test_Report | 【自测情况】 | |
| Test_Suggestion | 【建议】 | |
| Comment | 【查找问题的思路】 | 如为空则填写"请填写" |
| *(Redmine 登录用户)* | 解决者 | 当前登录用户 |
| *(固定值)* | 状态 → Fixed | |
| *(固定值)* | 完成度 → 100% | |

## 安装

### 环境要求

- Ubuntu 桌面系统（需要系统托盘支持）
- Python 3.6+
- PyQt5、requests

### 安装步骤

```bash
cd syncRedmine/
chmod +x install.sh
./install.sh
```

安装脚本会：
1. 安装 Python 依赖（`requests`、`PyQt5`）
2. 复制程序到 `~/.local/share/syncRedmine/`
3. 创建开机自启动配置（`~/.config/autostart/syncRedmine.desktop`）
4. 询问是否立即启动

### 首次启动

首次启动会弹出账号配置窗口，需填写：

- **Gerrit**：服务器地址、用户名、密码（用于 REST API 检测 push）
- **Redmine**：服务器地址、用户名、密码（用于同步问题字段）

配置保存在 `~/.commit_tool/sync_config.json`（密码 base64 编码）。

## 使用

### 日常使用

安装后无需任何操作，程序开机自动启动并常驻系统托盘：

1. 正常使用 commit 工具提交代码
2. push 完成后 syncRedmine 自动弹出确认框
3. 确认信息无误后点击 **「是，立即同步」**
4. 同步结果即时显示

### 托盘图标状态

| 图标颜色 | 含义 |
|---|---|
| 蓝色 | 空闲，监听中 |
| 橙色 | 检测到新提交，等待 push 完成 |
| 绿色（带感叹号） | push 完成，等待用户确认同步 |

### 右键菜单

- **配置账号**：重新修改 Gerrit / Redmine 的账号密码
- **退出**：关闭程序

### 命令行

```bash
# 启动程序（常驻托盘）
python3 ~/.local/share/syncRedmine/syncRedmine.py &

# 仅打开账号配置
python3 ~/.local/share/syncRedmine/syncRedmine.py --setup
```

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
| `~/.commit_tool/sync_config.json` | 账号配置（密码 base64 编码） |
| `~/commit_data.log` | commit 工具写入的提交日志（只读监控） |
