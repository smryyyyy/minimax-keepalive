<h1 align="center">MiniMax 会话保活</h1>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.9+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Flask-3.x-000000?style=flat-square&logo=flask&logoColor=white" alt="Flask">
  <img src="https://img.shields.io/badge/Platform-Linux%20%7C%20macOS-lightgrey?style=flat-square" alt="Platform">
  <img src="https://img.shields.io/badge/Memory-<100MB-brightgreen?style=flat-square" alt="Memory">
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="MIT License">
</p>

<p align="center">
  定期调用 MiniMax Code API 保持云端会话活跃 · 单进程 Web 仪表盘一键管理。
</p>

---

## 目录

- [功能特点](#功能特点)
- [快速开始](#快速开始)
- [架构](#架构)
- [部署方式](#部署方式)
- [配置说明](#配置说明)
- [项目结构](#项目结构)
- [技术栈](#技术栈)
- [许可证](#许可证)

---

## 功能特点

- **单进程架构**：Web + 保活在同一个 Python 进程，保活跑在后台线程，通过 `threading.Event` 控制启停，崩溃自动拉起
- **JWT 鉴权**：直接用 MiniMax Code 接口的 `?token=JWT` 鉴权机制，免抓 Cookie
- **保存即校验**：Web 改完 Token → 主线程立即跑一次心跳 → 5 秒后仪表盘自动显示新结果，不用傻等 5 分钟
- **状态 100% 准确**：Web 显示直接读线程标志位 + on_tick 回调，不会"显示运行但实际死了"
- **Web 仪表盘**：密码登录，看状态 / 改 Token / 启停 / 看实时日志，5 秒自动刷新
- **智能容错**：Token 失效优雅告警、Token 文件丢失不退出（恢复后自动生效）、网络抖动自动重试
- **极低资源**：内存 < 100MB，CPU 几乎为 0，2 核 4G 服务器随便跑
- **Token 临期警告**：剩余 < 7 天 Web 仪表盘自动出现黄点
- **零手动配置**：首次启动自动建文件、生成密码（打到 stderr）、绑定随机端口可选

## 快速开始

### 前置条件

- Python 3.9+
- 3 个依赖：`requests` / `flask` / `werkzeug`

### 三步跑起来

```bash
# 1. 装依赖
pip3 install -r requirements.txt

# 2. 抓 Token（Mac Chrome）
#    打开 https://agent.minimaxi.com → F12 → Network → 任意带 ?token= 的请求
#    复制 Query String 里 token 参数的值（整段 JWT）

# 3. 启动（脚本所在目录就是数据目录，不用 sudo）
python3 keepalive-web.py
# 首次启动会打印初始密码到 stderr（形如 "初始密码: s-0BmGBUHy_qog"）
# 浏览器打开 http://localhost:35235 → 用上面那个密码登录 → 在「更新 Token」粘 JWT → 保存
```

> 想要换端口：`python3 keepalive-web.py --port 12345`
> 想要预设密码：`python3 keepalive-web.py --password "your-password"`

## 架构

### 单进程（默认且唯一推荐）

```
┌────────────────────────────────────────┐
│  python3 keepalive-web.py              │
│  ┌──────────────────────────────────┐  │
│  │  Flask Web  (主线程)             │  │
│  │  - 登录 / 状态 / 控制 / 日志       │  │
│  │  - 改 Token → 立即触发一次 tick  │  │
│  └──────────────────────────────────┘  │
│  ┌──────────────────────────────────┐  │
│  │  保活循环  (后台线程)            │  │
│  │  - 5 min 调一次 MiniMax API      │  │
│  │  - Event 控制启停(0 内存开销)    │  │
│  │  - 实时读 token.txt              │  │
│  └──────────────────────────────────┘  │
└────────────────────────────────────────┘
        ↓                       ↓
   浏览器 :35235            MiniMax API
```

**核心交互**：

- 用户点"停止" → `Event.set()` → 线程在下一次 sleep 检查时退出
- 用户点"启动" → 新建线程 + `Event.clear()` → 循环立即跑
- 用户点"重启" → stop + sleep(1) + start
- **用户改 Token → 写文件 + 主线程立即跑一次 `tick()` → 状态/日志 5 秒内可见**

### 状态机

`tick()` 返回值直接决定 UI 状态：

| 返回 | 含义 | UI 显示 |
|------|------|---------|
| `ok` | 至少一个端点 200 | 🟢 绿点 + "运行中" |
| `token_invalid` | 401，JWT 被服务端作废 | 🔴 红点 + 引导重新抓 token |
| `no_token` | 文件不存在或为空 | 🟡 黄点 + 引导填 token |
| `server_error` | 5xx | 🟠 橙点 + 提示下一轮重试 |
| `network_error` | 连不上 | 🟠 橙点 + 提示下一轮重试 |
| `failed` | 候选端点全 401/403/404 | 🔴 红点 + 看日志排查 |

## 部署方式

### 方式一：宝塔 Python 项目管理器（推荐国内服务器）

> 单进程架构：Web 启动时自动拉起保活后台线程，**1 个项目搞定所有事**，不用写重启脚本也不用 计划任务。

#### 文件部署

把 `keepalive.py` / `keepalive-web.py` / `requirements.txt` 三个文件放到服务器任意目录。下面用 `<项目路径>` 占位符（宝塔添加项目时让你填什么就是什么，常见 `/www/wwwroot/minimax-keepalive/`，也可以放 `/opt/`、`/home/`、挂载盘等）：

```bash
# 本机
scp keepalive.py keepalive-web.py requirements.txt root@<服务器IP>:<项目路径>/
```

#### 宝塔添加项目

宝塔面板 → **Python 项目** → **添加项目**：

| 字段 | 填什么 |
|------|--------|
| 名称 | `minimax-keepalive` |
| 路径 | `<项目路径>`（你刚 scp 上去的那个目录）|
| Python 版本 | 系统默认（3.9+） |
| 框架 | **其他** |
| 启动方式 | **python** |
| 启动文件 | `keepalive-web.py` |
| **启动参数** | `--host 0.0.0.0 --port 35235` |
| 端口 | `35235` |
| 是否守护进程 | ✅ 启用 |
| 自动启动 | ✅ 启用 |
| 启动用户 | 看你服务器习惯（root/www 都行） |

下一步 → **模块** → **从 requirements.txt 安装** → 选 `<项目路径>/requirements.txt` → 安装。

提交。宝塔会建 venv → 装依赖 → 启动 → 自动守护（Web 进程死了 1 分钟内拉起，保活线程随之重新跑）。

#### 获取初始密码

首次启动后：
- 宝塔 → Python 项目 → 找到 `minimax-keepalive` → **日志** → 找 `初始密码: xxxx`
- 浏览器打开 `http://<服务器IP>:35235` → 输密码登录
- 登录后第一件事：去「更新 Token」填 JWT

> ⚠️ 如果从公网访问不了，先去腾讯云控制台 → 安全组 → 放行 TCP:35235（宝塔防火墙只是其中一层）。

### 方式二：systemd（Linux 通用）

```ini
# /etc/systemd/system/minimax-keepalive.service
[Unit]
Description=MiniMax Keepalive Web
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 <项目路径>/keepalive-web.py --host 0.0.0.0 --port 35235
Restart=always
RestartSec=10
User=root
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=<项目路径>

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now minimax-keepalive
systemctl status minimax-keepalive
```

### 方式三：手动 nohup

```bash
# 启动（只跑 web，保活是 web 进程内的线程）
nohup python3 keepalive-web.py --host 0.0.0.0 --port 35235 > /tmp/minimax-keepalive.log 2>&1 &

# 停止
pkill -TERM -f keepalive-web.py

# 看日志（默认在脚本同目录的 minimax-keepalive.log）
tail -f <项目路径>/minimax-keepalive.log
```

## 配置说明

### 路径约定

所有数据文件默认在**脚本同目录**（`os.path.dirname(os.path.abspath(__file__))`），可用环境变量覆盖：

| 文件 | 默认位置 | 环境变量覆盖 | 权限 |
|------|---------|-------------|------|
| `keepalive.py` | 脚本目录 | — | 755 |
| `keepalive-web.py` | 脚本目录 | — | 755 |
| `requirements.txt` | 脚本目录 | — | 644 |
| `token.txt` | 脚本目录 | `MINIMAX_TOKEN_FILE` | **600** |
| `.web-pass` | 脚本目录 | `MINIMAX_PASS_FILE` | 600 |
| `.web-secret` | 脚本目录 | `MINIMAX_SECRET_FILE` | 600 |
| `minimax-keepalive.log` | 脚本目录 | `MINIMAX_LOG_FILE` | 644 |

> 不再写 `/var/log/`：避免权限问题；服务器上用 `MINIMAX_LOG_FILE` 指向 `/var/log/...` 即可。

### Token 获取

1. Mac Chrome 打开 <https://agent.minimaxi.com>，正常登录
2. F12 打开 DevTools → 切到 **Network** 标签
3. F5 刷新页面
4. 列表里找任意 `agent.minimaxi.com` 的请求 → 点击
5. 右侧 Headers 面板 → 找 **Request URL**（含 query string）
6. 复制 `?token=eyJ...` 里 `token=` 后面的整段值
7. 粘贴到 Web 仪表盘「更新 Token」保存即可（**不要**直接编辑 `token.txt`，Web 会立即校验新 token 是否有效）

或者在 Web 仪表盘「更新 Token」卡片标题旁的 `?` 悬停看 5 步教程。

### Web 端口与启动参数

```bash
# 默认 0.0.0.0:35235（公网可访问，需密码保护）
python3 keepalive-web.py

# 自定义端口
python3 keepalive-web.py --port 12345

# 仅本机访问（SSH 隧道场景）
python3 keepalive-web.py --host 127.0.0.1

# 自定义初始密码（首次启动用，之后会被 .web-pass 覆盖）
python3 keepalive-web.py --password "your-password"
```

### 轮询间隔

编辑 `keepalive.py` 顶部：

```python
INTERVAL_SECONDS = 300  # 默认 5 分钟
```

## 项目结构

```
.
├── keepalive.py             # 保活核心（线程循环 + JWT 鉴权 + 容错 + CLI 入口）
├── keepalive-web.py         # Flask Web + 后台线程（单进程入口）
├── requirements.txt         # 依赖列表
├── README.md                # 本文档
├── token.txt                # 首次启动自动创建（空文件），Web 界面填
├── .web-pass                # 首次启动自动生成（密码哈希）
├── .web-secret              # 首次启动自动生成（Flask session 密钥）
└── minimax-keepalive.log    # 保活日志
```

> 部署到服务器后，**所有数据文件都在启动目录里**，删了项目目录就连带清理干净。

## 技术栈

| 组件 | 用途 |
|------|------|
| Python 3.9+ | 主语言 |
| requests | HTTP 调用（带连接复用 + 代理穿透） |
| Flask 3.x | Web 界面 |
| werkzeug | 密码哈希 + Session |
| threading | 后台保活线程 + `Event` 控制启停 |
| signal | SIGTERM/SIGINT 优雅退出（CLI 模式） |

接口细节：
- 保活接口：`GET https://agent.minimaxi.com/v1/api/user/info?token=JWT&...`
- 鉴权方式：JWT 放在 URL query，不依赖 Cookie
- 候选端点：`/v1/api/user/info` / `/v1/api/config/web/common_config` / `/matrix/api/v1/user/get_user_extra_info`

## 许可证

MIT License

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
