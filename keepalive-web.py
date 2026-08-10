#!/usr/bin/env python3
"""
MiniMax Keepalive Web 管理界面(v3 - 单进程版)
===============================================

- 单密码登录(SHA256 哈希存文件)
- 仪表盘:服务状态 / 控制 / Token 更新 / 密码修改 / 实时日志
- 保活循环在 Web 进程的**后台线程**里跑,通过 threading.Event 控制启停
  - 不再 pgrep / pkill / nohup,直接操作内存里的标志位
  - 状态显示 = 线程真实状态,不会有"显示运行但实际死了"的情况
- Kami 设计风格:暖米纸画布 + 油墨蓝强调色
- 每 5 秒自动刷新

启动:
    pip3 install flask
    sudo python3 /opt/minimax-keepalive-web.py

首次启动会自动生成随机密码并打印到 stderr。
"""

import os
import sys
import json
import time
import secrets
import argparse
import threading
from datetime import datetime
from functools import wraps
from flask import (
    Flask, request, session, redirect, url_for,
    render_template_string, jsonify,
)
from werkzeug.security import generate_password_hash, check_password_hash

# 从 keepalive 模块导入核心逻辑(单进程架构关键)
from keepalive import (
    run_keepalive_forever, tick,
    load_token, jwt_exp_unix, format_remaining,
    setup_logging, ensure_token_file,
    TOKEN_FILE, LOG_FILE,
)


# ===================== 路径配置 =====================
# 默认:跟脚本同目录
# 想要放别处用环境变量覆盖:
#   MINIMAX_PASS_FILE=/path/to/.web-pass
#   MINIMAX_SECRET_FILE=/path/to/.web-secret
#   MINIMAX_TOKEN_FILE=/path/to/token.txt  (从 keepalive 模块继承)
#   MINIMAX_LOG_FILE=/path/to/keepalive.log (从 keepalive 模块继承)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PASS_FILE = os.environ.get("MINIMAX_PASS_FILE") or os.path.join(SCRIPT_DIR, ".web-pass")
SECRET_FILE = os.environ.get("MINIMAX_SECRET_FILE") or os.path.join(SCRIPT_DIR, ".web-secret")


# ===================== 启动参数 =====================
parser = argparse.ArgumentParser(description="MiniMax Keepalive Web 管理界面")
parser.add_argument("--host", default="0.0.0.0", help="监听地址(默认 0.0.0.0)")
parser.add_argument("--port", type=int, default=35235, help="监听端口(默认 35235)")
parser.add_argument("--password", help="设置/重置管理密码(也可在 Web 界面改)")
parser.add_argument("--reset-password", action="store_true", help="强制重置密码")
args = parser.parse_args()


# ===================== Flask app =====================
app = Flask(__name__)

# Session secret
try:
    with open(SECRET_FILE, "rb") as f:
        app.secret_key = f.read()
except FileNotFoundError:
    app.secret_key = secrets.token_bytes(32)
    with open(SECRET_FILE, "wb") as f:
        f.write(app.secret_key)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass


# ===================== 密码管理 =====================
def set_password(new_password):
    """设置密码,存哈希。"""
    h = generate_password_hash(new_password)
    with open(PASS_FILE, "w") as f:
        f.write(h)
    try:
        os.chmod(PASS_FILE, 0o600)
    except OSError:
        pass


def password_exists():
    return os.path.exists(PASS_FILE)


def verify_password(p):
    if not p:
        return False
    try:
        with open(PASS_FILE, "r") as f:
            return check_password_hash(f.read().strip(), p)
    except Exception:
        return False


# 处理密码初始化
initial_password_printed = None
if args.reset_password or (args.password and not password_exists()):
    pwd = args.password or secrets.token_urlsafe(10)
    set_password(pwd)
    initial_password_printed = pwd
elif args.password:
    set_password(args.password)
    initial_password_printed = args.password
elif not password_exists():
    pwd = secrets.token_urlsafe(10)
    set_password(pwd)
    initial_password_printed = pwd


# ===================== 保活线程管理 =====================
# 单进程架构:保活在 Web 进程里的后台线程,通过 Event 控制启停
_keepalive_stop_event = threading.Event()  # set = 已停止
_keepalive_thread = None
_keepalive_lock = threading.Lock()
_web_logger = None  # 延迟初始化(setup_logging 有副作用,等第一次启动时调用)

# 线程状态(由 tick 回调更新,跟日志完全同步,前端 5 秒拉一次)
# status ∈ {"ok", "no_token", "token_invalid", "server_error", "network_error", "failed", "error", "unknown"}
_keepalive_state = {
    "status": "unknown",
    "path": None,
    "status_code": None,
    "tick_time": 0,  # unix timestamp of last tick
    "last_ok_time": 0,  # unix timestamp of last successful tick
}
_state_lock = threading.Lock()


def _on_tick_callback(result, path, status_code):
    """run_keepalive_forever 调用的回调,把 tick 结果同步到 _keepalive_state"""
    with _state_lock:
        _keepalive_state["status"] = result
        _keepalive_state["path"] = path
        _keepalive_state["status_code"] = status_code
        _keepalive_state["tick_time"] = time.time()
        if result == "ok":
            _keepalive_state["last_ok_time"] = time.time()


def _ensure_logger():
    global _web_logger
    if _web_logger is None:
        _web_logger = setup_logging()
    return _web_logger


def start_keepalive_thread():
    """启动保活线程(幂等)。"""
    global _keepalive_thread
    with _keepalive_lock:
        if _keepalive_thread and _keepalive_thread.is_alive():
            return False
        _keepalive_stop_event.clear()
        logger = _ensure_logger()
        _keepalive_thread = threading.Thread(
            target=run_keepalive_forever,
            args=(_keepalive_stop_event, logger),
            kwargs={"on_tick": _on_tick_callback},
            name="keepalive-loop",
            daemon=True,
        )
        _keepalive_thread.start()
        return True


def stop_keepalive_thread():
    """停止保活线程(标志位置位,循环在下一次检查时退出)。"""
    _keepalive_stop_event.set()


def get_keepalive_status():
    """读线程状态 + 真实心跳结果,跟日志完全同步。"""
    thread_alive = bool(_keepalive_thread and _keepalive_thread.is_alive())
    with _state_lock:
        state = dict(_keepalive_state)  # 拷贝一份,避免跨线程读 dict 异常
    return {
        "running": thread_alive and not _keepalive_stop_event.is_set(),
        "pid": os.getpid(),  # 当前进程 PID
        "thread_alive": thread_alive,
        # 跟日志完全同步的心跳状态
        "last_status": state["status"],  # ok / token_invalid / no_token / ...
        "last_path": state["path"],
        "last_status_code": state["status_code"],
        "last_tick_time": state["tick_time"],
        "last_ok_time": state["last_ok_time"],
    }


def control_keepalive(action):
    """通过 event 控制保活循环。"""
    if action == "stop":
        stop_keepalive_thread()
        return {"ok": True, "message": "已停止(后台线程存活,5 秒内停止心跳)"}
    elif action == "start":
        started = start_keepalive_thread()
        if started:
            return {"ok": True, "message": "已启动(下一轮心跳立即触发)"}
        return {"ok": True, "message": "已在运行,无需启动"}
    elif action == "restart":
        stop_keepalive_thread()
        time.sleep(1)  # 给当前 sleep 退出的时间
        started = start_keepalive_thread()
        if started:
            return {"ok": True, "message": "已重启(下一轮心跳立即触发)"}
        return {"ok": False, "message": "重启失败"}
    else:
        return {"ok": False, "message": f"未知动作: {action}"}


# 共享 session,给"立即触发 tick"用,避免每次新建连接池
_tick_session = None
_tick_session_lock = threading.Lock()


def _get_tick_session():
    global _tick_session
    with _tick_session_lock:
        if _tick_session is None:
            import requests  # 延迟 import,避免没装 requests 时 web 起不来
            _tick_session = requests.Session()
            _tick_session.trust_env = False
        return _tick_session


def trigger_immediate_tick():
    """
    在主线程里立即跑一次 tick(),不依赖后台保活循环的 5 分钟节奏。
    用于:保存 token 后立刻校验新 token 是否可用。
    状态通过 on_tick 回调同步到 _keepalive_state,前端 5 秒后能看到结果。
    """
    logger = _ensure_logger()
    session = _get_tick_session()
    try:
        token = load_token()
    except RuntimeError as e:
        logger.error(f"立即校验跳过: {e}")
        _on_tick_callback("no_token", None, None)
        return "no_token"
    try:
        return tick(session, token, logger, on_tick=_on_tick_callback)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"立即校验异常: {e}")
        _on_tick_callback("error", None, None)
        return "error"


# ===================== Auth 装饰器 =====================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ===================== Token / 日志 辅助 =====================

def parse_token_jwt(token):
    """解析 JWT,返回 {exp, exp_human, remaining_days, length}。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(__import__('base64').urlsafe_b64decode(payload_b64))
        exp = int(payload.get("exp", 0))
        if not exp:
            return None
        return {
            "length": len(token),
            "exp": exp,
            "exp_human": datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S"),
            "remaining_days": max(0, (exp - time.time()) / 86400),
        }
    except Exception:
        return None


def read_token_info():
    try:
        with open(TOKEN_FILE, "r") as f:
            token = f.read().strip()
    except FileNotFoundError:
        return {"exists": False}
    if not token:
        return {"exists": True, "empty": True, "length": 0}
    info = parse_token_jwt(token)
    if info:
        return {"exists": True, "empty": False, **info}
    return {"exists": True, "empty": False, "length": len(token), "parse_error": True}


def read_logs(n=50):
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
            return [l.rstrip() for l in lines[-n:]]
    except FileNotFoundError:
        return ["(日志文件不存在)"]
    except Exception as e:
        return [f"(读取失败: {e})"]


# ===================== HTML 模板(Kami 设计) =====================

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>MiniMax 保活 · 登录</title>
<style>
:root {
  --canvas:#f5f4ed; --ivory:#faf9f5; --sand:#e8e6dc;
  --ink:#1B365D; --ink-light:#2D5A8A; --near-black:#141413;
  --olive:#504e49; --stone:#6b6a64; --error:#B53333;
  --border-warm:#d4d2c5; --tint-light:#EEF2F7;
  --serif:"Charter","Georgia","PingFang SC","Songti SC","Times New Roman",serif;
  --sans:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Helvetica Neue",sans-serif;
  --mono:"SF Mono",Monaco,Menlo,monospace;
}
*{box-sizing:border-box}
body{font-family:var(--sans);background:var(--canvas);color:var(--near-black);margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;-webkit-font-smoothing:antialiased}
.box{width:100%;max-width:380px;background:var(--ivory);border:1px solid #e8e6dc;border-radius:16px;padding:40px 36px 32px;box-shadow:0 4px 24px rgba(20,20,19,.04)}
.eyebrow{font-size:10.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--olive);margin:0 0 8px;font-weight:600}
h1{font-family:var(--serif);font-weight:500;font-size:30px;margin:0 0 10px;color:var(--near-black);letter-spacing:-.01em;line-height:1.1}
h1 .cn{color:var(--ink);margin-left:2px}
.sub{font-size:13px;color:var(--olive);margin:0 0 28px;line-height:1.55}
input[type=password]{width:100%;padding:11px 14px;border:1px solid var(--border-warm);border-radius:6px;background:var(--canvas);color:var(--near-black);font-family:var(--mono);font-size:13px;transition:border-color .15s,box-shadow .15s}
input[type=password]:focus{outline:0;border-color:var(--ink);box-shadow:0 0 0 3px var(--tint-light)}
button{width:100%;margin-top:14px;padding:12px;background:var(--ink);color:var(--ivory);border:1px solid var(--ink);border-radius:8px;font-size:13px;font-weight:500;cursor:pointer;font-family:var(--sans);transition:background .15s}
button:hover{background:var(--ink-light);border-color:var(--ink-light)}
.err{color:var(--error);font-size:12px;margin:12px 0 0;text-align:center}
</style>
</head>
<body>
<div class="box">
  <p class="eyebrow">服务控制台</p>
  <h1>MiniMax<span class="cn">·</span>保活</h1>
  <p class="sub">会话保活 · 管理控制台</p>
  <form method="POST">
    <input type="password" name="password" placeholder="管理密码" autofocus required>
    <button type="submit">登录</button>
    {% if error %}<p class="err">{{ error }}</p>{% endif %}
  </form>
</div>
</body>
</html>"""


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>MiniMax 保活</title>
<style>
:root {
  --canvas:#f5f4ed; --ivory:#faf9f5; --sand:#e8e6dc;
  --ink:#1B365D; --ink-light:#2D5A8A; --near-black:#141413;
  --olive:#504e49; --stone:#6b6a64; --error:#B53333;
  --warn:#b88a2e; --ok:#6b8e23;
  --border:#e8e6dc; --border-warm:#d4d2c5;
  --tint:#E4ECF5; --tint-light:#EEF2F7;
  --serif:"Charter","Georgia","PingFang SC","Songti SC","Times New Roman",serif;
  --sans:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Helvetica Neue",sans-serif;
  --mono:"SF Mono",Monaco,Menlo,"Cascadia Code",monospace;
}
*{box-sizing:border-box}
body{font-family:var(--sans);background:var(--canvas);color:var(--near-black);margin:0;padding:0;font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased}
.page{max-width:920px;margin:0 auto;padding:48px 32px 80px}
header{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:40px;padding-bottom:20px;border-bottom:.5px solid var(--border)}
.title-block .eyebrow{font-size:10.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--olive);margin:0 0 6px;font-weight:600}
h1{font-family:var(--serif);font-weight:500;font-size:32px;margin:0;letter-spacing:-.01em;line-height:1.1}
h1 .cn{color:var(--ink);margin-left:2px}
.logout{font-size:12px;color:var(--olive);text-decoration:none;border-bottom:1px solid transparent;padding-bottom:1px;transition:all .15s}
.logout:hover{color:var(--ink);border-bottom-color:var(--ink)}
.card{background:var(--ivory);border:1px solid var(--border);border-radius:12px;padding:24px 28px;margin-bottom:20px}
.card h2{font-family:var(--serif);font-weight:500;font-size:17px;margin:0 0 4px;color:var(--near-black);letter-spacing:-.005em}
.card .hint{font-size:12.5px;color:var(--olive);margin:0 0 18px;line-height:1.5}
.card h2+.hint{margin-top:4px}
.status-grid{display:flex;flex-direction:column;gap:0}
.status-row{display:flex;justify-content:space-between;align-items:baseline;padding:11px 0;border-bottom:.5px dashed var(--border-warm);font-size:13px;gap:16px}
.status-row:last-child{border-bottom:0}
.status-row .label{color:var(--olive);font-size:12.5px;letter-spacing:.2px;white-space:nowrap}
.status-row .value{font-family:var(--serif);color:var(--near-black);text-align:right}
.status-row code{font-family:var(--mono);font-size:11.5px;color:var(--ink);background:var(--tint-light);padding:2px 7px;border-radius:3px;word-break:break-all;max-width:60%;text-align:right}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:middle}
.dot.on{background:var(--ok)}
.dot.off{background:var(--error)}
.dot.warn{background:var(--warn)}
.metric{display:inline-flex;align-items:baseline;gap:2px}
.metric .num{font-family:var(--serif);font-size:18px;font-weight:500;color:var(--ink);letter-spacing:-.01em}
.metric .unit{font-size:11.5px;color:var(--olive);margin:0 8px 0 2px}
.button-row{display:flex;gap:10px;flex-wrap:wrap}
.btn{display:inline-block;padding:9px 18px;border-radius:8px;font-size:13px;font-weight:500;font-family:var(--sans);border:1px solid var(--border-warm);background:var(--sand);color:var(--near-black);cursor:pointer;text-decoration:none;transition:all .15s}
.btn:hover{background:var(--border-warm)}
.btn.btn-primary{background:var(--ink);color:var(--ivory);border-color:var(--ink)}
.btn.btn-primary:hover{background:var(--ink-light);border-color:var(--ink-light)}
.btn.btn-danger{background:var(--ivory);color:var(--error);border-color:var(--error)}
.btn.btn-danger:hover{background:#fcecec}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn:disabled:hover{background:var(--sand)}
.btn.btn-primary:disabled:hover{background:var(--ink)}
.btn.btn-danger:disabled:hover{background:var(--ivory)}
.btn .icon{display:inline-block;margin-right:6px;font-size:14px;line-height:1;vertical-align:-1px}
textarea,input[type=password],input[type=text]{width:100%;padding:10px 12px;border:1px solid var(--border-warm);border-radius:6px;background:var(--canvas);color:var(--near-black);font-family:var(--mono);font-size:12px;line-height:1.5;resize:vertical;transition:border-color .15s,box-shadow .15s}
textarea{min-height:64px}
textarea:focus,input:focus{outline:0;border-color:var(--ink);box-shadow:0 0 0 2px var(--tint-light)}
.actions{display:flex;gap:10px;margin-top:12px;align-items:center}
.actions .msg{font-size:12px;color:var(--olive);margin-left:auto;font-style:italic}
.log-box{background:#141413;color:#d4d2c5;border-radius:8px;padding:16px 18px;font-family:var(--mono);font-size:11.5px;line-height:1.65;max-height:360px;overflow-y:auto;white-space:pre;word-wrap:break-word}
.log-box .ok{color:#95c378}
.log-box .warn{color:#d6a857}
.log-box .err{color:#e5816a}
.muted{color:var(--olive);font-size:11.5px;font-weight:400;margin-left:8px;font-family:var(--mono)}
.status-line{display:inline-flex;align-items:center;gap:6px;font-family:var(--sans);font-size:13px}
.help{display:inline-flex;align-items:center;justify-content:center;width:17px;height:17px;border-radius:50%;background:var(--sand);color:var(--olive);font-size:11px;font-weight:600;margin-left:8px;cursor:help;position:relative;vertical-align:1px;font-family:var(--sans);transition:all .15s;user-select:none}
.help:hover,.help:focus{background:var(--ink);color:var(--ivory);outline:0}
.tooltip{position:absolute;top:calc(100% + 12px);left:-8px;width:340px;background:var(--ivory);border:1px solid var(--border-warm);border-radius:8px;padding:14px 18px;box-shadow:0 4px 24px rgba(20,20,19,.08);font-size:12.5px;line-height:1.55;color:var(--near-black);font-weight:400;text-align:left;font-family:var(--sans);opacity:0;pointer-events:none;transition:opacity .15s;z-index:100}
.help:hover .tooltip,.help:focus .tooltip{opacity:1;pointer-events:auto}
.tooltip::before{content:"";position:absolute;top:-5px;left:14px;width:9px;height:9px;background:var(--ivory);border-left:1px solid var(--border-warm);border-top:1px solid var(--border-warm);transform:rotate(45deg)}
.tooltip strong{display:block;font-family:var(--serif);font-weight:500;color:var(--ink);font-size:13.5px;margin:0 0 10px;letter-spacing:-.005em}
.tooltip ol{margin:0;padding-left:18px}
.tooltip ol li{margin:5px 0;color:var(--near-black)}
.tooltip code{font-family:var(--mono);font-size:11.5px;color:var(--ink);background:var(--tint-light);padding:1px 5px;border-radius:3px}
.tooltip kbd{font-family:var(--mono);font-size:10.5px;color:var(--ink);background:var(--sand);border:1px solid var(--border-warm);border-radius:3px;padding:1px 5px;margin:0 1px}
.tooltip b{color:var(--ink);font-weight:600}
</style>
</head>
<body>
<div class="page">
  <header>
    <div class="title-block">
      <p class="eyebrow">服务控制台</p>
      <h1>MiniMax<span class="cn">·</span>保活</h1>
    </div>
    <a href="/logout" class="logout">退出</a>
  </header>

  <div class="card">
    <h2>服务状态</h2>
    <p class="hint">保活进程运行情况,每 5 秒自动刷新</p>
    <div class="status-grid" id="status">
      <div class="status-row"><span class="label">加载中</span><span class="value">…</span></div>
    </div>
  </div>

  <div class="card">
    <h2>控制</h2>
    <p class="hint">根据当前状态自动启用 / 禁用对应按钮,操作前会要求确认</p>
    <div class="button-row">
      <button class="btn btn-primary" data-action="start" onclick="control('start')"><span class="icon">▶</span>启动</button>
      <button class="btn" data-action="restart" onclick="control('restart')"><span class="icon">↻</span>重启</button>
      <button class="btn btn-danger" data-action="stop" onclick="control('stop')"><span class="icon">■</span>停止</button>
    </div>
  </div>

  <div class="card">
    <h2>更新 Token <span class="help" tabindex="0" aria-label="如何获取 Token">?<span class="tooltip"><strong>如何获取 Token</strong><ol><li>Chrome 打开 <code>agent.minimaxi.com</code> 并登录</li><li>按 <kbd>F12</kbd> → 切到 <b>Network</b> 标签 → <kbd>F5</kbd> 刷新</li><li>点任意 <code>agent.minimaxi.com</code> 请求</li><li>右侧 Headers → 找 <b>Request URL</b>,复制 <code>?token=</code> 后面的整段</li><li>粘到下面文本框 → 点保存</li></ol></span></span></h2>
    <p class="hint">粘贴新 JWT,保存后立即校验,5 秒后状态自动刷新</p>
    <textarea id="new-token" placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."></textarea>
    <div class="actions">
      <button class="btn btn-primary" onclick="updateToken()">保存 Token</button>
      <span class="msg" id="token-msg"></span>
    </div>
  </div>

  <div class="card">
    <h2>修改密码 <span class="muted">至少 6 位</span></h2>
    <p class="hint">修改后立即生效,会自动退出当前会话</p>
    <input type="password" id="new-pass" placeholder="新密码">
    <div class="actions">
      <button class="btn" onclick="updatePassword()">更新密码</button>
      <span class="msg" id="pass-msg"></span>
    </div>
  </div>

  <div class="card">
    <h2>实时日志 <span class="muted" id="log-meta"></span></h2>
    <p class="hint">最后 30 行,自动滚动到底部</p>
    <div class="log-box" id="logs">加载中...</div>
  </div>
</div>

<script>
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

async function refresh(){
  try{
    const r=await fetch('/api/status');
    if(r.status===401){location.href='/login';return}
    const d=await r.json();
    renderStatus(d);
    renderLogs();
  }catch(e){console.error(e)}
}

function row(label,value){return `<div class="status-row"><span class="label">${label}</span><span class="value">${value}</span></div>`}

function renderStatus(d){
  const k=d.keepalive, t=d.token, html=[];

  if(k.running){
    html.push(row('运行状态','<span class="status-line"><span class="dot on"></span>运行中</span>'));
    html.push(row('进程 PID',`<code>${esc(k.pid)}</code>`));
  }else{
    html.push(row('运行状态','<span class="status-line"><span class="dot off"></span>已停止</span>'));
  }

  // 心跳结果(跟日志完全同步,跟下面 Token 状态分开显示)
  if(k.last_status && k.last_status !== 'unknown'){
    const lastTick = k.last_tick_time ? secondsAgo(k.last_tick_time) : '从未';
    const lastOk = k.last_ok_time ? secondsAgo(k.last_ok_time) : '从未成功';
    let badge = '';
    let text = '';
    if(k.last_status === 'ok'){
      badge = '<span class="dot on"></span>';
      text = `<strong>${esc(k.last_path||'')}</strong> 200`;
    }else if(k.last_status === 'token_invalid'){
      badge = '<span class="dot off"></span>';
      text = `<strong>已失效</strong>(${esc(String(k.last_status_code||''))})`;
    }else if(k.last_status === 'no_token'){
      badge = '<span class="dot warn"></span>';
      text = 'Token 文件未配置';
    }else if(k.last_status === 'server_error'){
      badge = '<span class="dot warn"></span>';
      text = `服务端错误 ${esc(String(k.last_status_code||''))}`;
    }else if(k.last_status === 'network_error'){
      badge = '<span class="dot warn"></span>';
      text = '网络异常';
    }else{
      badge = '<span class="dot warn"></span>';
      text = esc(k.last_status);
    }
    html.push(row('心跳结果', `<span class="status-line">${badge}${text}</span> <span class="muted">· ${lastTick}前</span>`));
    html.push(row('上次成功', `<code>${lastOk}前</code>`));
  }

  if(t.exists && !t.empty && t.exp){
    const days=Math.floor(t.remaining_days);
    const hours=Math.floor((t.remaining_days-days)*24);
    // 关键修复:如果心跳结果是 token_invalid,Token 状态用红色
    let dotClass = 'on';
    if(k.last_status === 'token_invalid') dotClass = 'off';
    else if(t.remaining_days<7) dotClass = 'warn';
    let warnTag = `<span class="dot ${dotClass}"></span>`;
    const tagTitle = k.last_status === 'token_invalid' ? '服务器已拒绝(401/403),虽然 JWT 还没到期' : (t.remaining_days<7 ? '即将过期' : '');
    html.push(row('Token 剩余',`<span class="metric" title="${tagTitle}">${warnTag}<span class="num">${days}</span><span class="unit">天</span><span class="num">${hours}</span><span class="unit">小时</span></span>`));
    html.push(row('Token 过期',`<code>${esc(t.exp_human)}</code>`));
    html.push(row('Token 长度',`<code>${t.length} 字节</code>`));
  }else if(t.exists && t.empty){
    html.push(row('Token','<span class="status-line"><span class="dot warn"></span>文件为空</span>'));
  }else if(!t.exists){
    html.push(row('Token','<span class="status-line"><span class="dot off"></span>文件不存在</span>'));
  }else{
    html.push(row('Token','<span class="status-line"><span class="dot warn"></span>无法解析</span>'));
  }

  if(d.last_log && d.last_log.length>0){
    const last=d.last_log[d.last_log.length-1];
    html.push(row('最后日志',`<code>${esc(last.substring(0,80))}</code>`));
  }

  document.getElementById('status').innerHTML=html.join('');
  updateButtons(k.running);
}

function secondsAgo(unixTs){
  const s = Math.max(0, Math.floor(Date.now()/1000 - unixTs));
  if(s < 60) return s + '秒';
  if(s < 3600) return Math.floor(s/60) + '分钟';
  if(s < 86400) return Math.floor(s/3600) + '小时';
  return Math.floor(s/86400) + '天';
}

function updateButtons(running){
  const start=document.querySelector('[data-action="start"]');
  const stop=document.querySelector('[data-action="stop"]');
  const restart=document.querySelector('[data-action="restart"]');
  if(start) start.disabled=running;
  if(stop) stop.disabled=!running;
  if(restart) restart.disabled=!running;
}

async function renderLogs(){
  try{
    const r=await fetch('/api/logs?lines=30');
    if(r.status===401){location.href='/login';return}
    const d=await r.json();
    const box=document.getElementById('logs');
    const colored=d.lines.map(l=>{
      let cls='';
      if(l.includes('OK -')||l.includes('Token 加载成功')) cls='ok';
      else if(l.includes('TOKEN 失效')||l.includes('网络异常')||l.includes('启动时 token 读取失败')||l.includes('本轮跳过')||l.includes('启动失败')) cls='err';
      else if(l.includes('警告')||l.includes('WARN')||l.includes('warn')) cls='warn';
      return cls?`<span class="${cls}">${esc(l)}</span>`:esc(l);
    });
    box.innerHTML=colored.join('\n');
    box.scrollTop=box.scrollHeight;
    document.getElementById('log-meta').textContent=`(30 行 · 5s 刷新 · ${new Date().toLocaleTimeString()})`;
  }catch(e){console.error(e)}
}

async function control(action){
  const labels={start:'启动',stop:'停止',restart:'重启'};
  const btn=document.querySelector(`[data-action="${action}"]`);
  if(btn && btn.disabled){alert('当前状态不允许此操作');return}
  if(!confirm(`确认${labels[action]}保活服务?`)) return;
  const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
  const d=await r.json();
  alert(d.message||(d.ok?'成功':'失败'));
  setTimeout(refresh,1500);
}

async function updateToken(){
  const tk=document.getElementById('new-token').value.trim();
  if(!tk){alert('Token 不能为空');return}
  const r=await fetch('/api/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tk})});
  const d=await r.json();
  const msg=document.getElementById('token-msg');
  msg.textContent=d.message;
  if(d.ok){document.getElementById('new-token').value='';setTimeout(refresh,1000)}else{msg.classList.add('err');setTimeout(()=>msg.classList.remove('err'),4000)}
}

async function updatePassword(){
  const p=document.getElementById('new-pass').value;
  if(!p||p.length<6){alert('密码至少 6 位');return}
  if(!confirm('确认修改密码?会强制重新登录'))return;
  const r=await fetch('/api/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p})});
  const d=await r.json();
  alert(d.message);
  if(d.ok){setTimeout(()=>location.href='/login',1200)}
}

refresh();
setInterval(refresh,5000);
</script>
</body>
</html>"""


# ===================== 路由 =====================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        p = request.form.get("password", "")
        if verify_password(p):
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        return render_template_string(LOGIN_HTML, error="密码错误")
    return render_template_string(LOGIN_HTML, error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/status")
@login_required
def api_status():
    return jsonify({
        "keepalive": get_keepalive_status(),
        "token": read_token_info(),
        "last_log": read_logs(3),
    })


@app.route("/api/logs")
@login_required
def api_logs():
    n = int(request.args.get("lines", 30))
    return jsonify({"lines": read_logs(n)})


@app.route("/api/control", methods=["POST"])
@login_required
def api_control():
    data = request.get_json(silent=True) or request.form
    action = data.get("action")
    if action not in ("start", "stop", "restart"):
        return jsonify({"ok": False, "message": "未知动作"}), 400
    return jsonify(control_keepalive(action))


@app.route("/api/token", methods=["POST"])
@login_required
def api_token():
    data = request.get_json(silent=True) or request.form
    new_token = (data.get("token") or "").strip()
    if not new_token:
        return jsonify({"ok": False, "message": "Token 不能为空"}), 400
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(new_token)
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

    # 立即触发一次 tick,前端 5 秒后能看到新状态(避免 5 分钟延迟)
    status = trigger_immediate_tick()
    if status == "ok":
        return jsonify({"ok": True, "message": "已更新,Token 有效 ✓"})
    elif status == "token_invalid":
        return jsonify({"ok": False, "message": "已保存,但服务端返回 401,Token 已被作废,需在 Mac 重新抓"})
    elif status == "no_token":
        return jsonify({"ok": False, "message": "已保存,但读取失败"})
    elif status in ("server_error", "network_error", "failed"):
        return jsonify({"ok": True, "message": f"已保存,本轮校验: {status}(可能是临时网络问题,下一轮会自动重试)"})
    else:
        return jsonify({"ok": True, "message": f"已保存,本轮校验: {status}"})


@app.route("/api/password", methods=["POST"])
@login_required
def api_password():
    data = request.get_json(silent=True) or request.form
    new_pass = (data.get("password") or "").strip()
    if not new_pass or len(new_pass) < 6:
        return jsonify({"ok": False, "message": "密码至少 6 位"}), 400
    try:
        set_password(new_pass)
        return jsonify({"ok": True, "message": "密码已更新,1.2 秒后跳到登录页"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


# ===================== 启动 =====================
if __name__ == "__main__":
    # 启动前:确保 token 文件存在(空文件也行,Web 界面会引导填)
    token_status = ensure_token_file()
    if token_status == "created":
        print(f"\n⚠️  Token 文件不存在,已自动创建空文件: {TOKEN_FILE}", file=sys.stderr)
        print(f"   登录 Web 后在「更新 Token」填入即可,无需重启\n", file=sys.stderr)

    # 单进程架构:Web 启动时拉起保活线程
    start_keepalive_thread()

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"🌐 MiniMax 保活 Web 界面", file=sys.stderr)
    print(f"   地址: http://{args.host}:{args.port}", file=sys.stderr)
    if initial_password_printed:
        print(f"\n   🔐 初始密码: {initial_password_printed}", file=sys.stderr)
        print(f"   (已加密保存到 {PASS_FILE},登录后可在界面修改)", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
