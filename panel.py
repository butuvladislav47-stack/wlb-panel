#!/usr/bin/env python3
import base64
import hashlib
import hmac
import html
import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "alpha 0.0.1"
APP_NAME = "WLB Panel"
HOST = os.environ.get("WLB_HOST", "0.0.0.0")
PORT = int(os.environ.get("WLB_PORT", "8088"))

BASE_DIR = Path("/var/lib/wlb-panel")
SESSIONS_DIR = BASE_DIR / "sessions"
LOG_DIR = Path("/var/log/wlb-panel")
CONFIG_DIR = Path("/etc/wlb-panel")
CONFIG_PATH = CONFIG_DIR / "config.json"
COOKIES_PATH = CONFIG_DIR / "wb-cookies.json"
PANEL_DIR = Path("/opt/wlb-panel")
CREATOR_BIN = Path("/opt/whitelist-bypass/headless-wbstream-creator")
CHROME_PROFILE = BASE_DIR / "chrome-profile"
BROWSER_LOG = LOG_DIR / "browser.log"
BROWSER_STATE = BASE_DIR / "browser.json"
DISPLAY_NUM = ":99"
VNC_PORT = 5901
NOVNC_PORT = 6080
CDP_PORT = 9222
WB_LOGIN_URL = "https://stream.wb.ru/login"
REQUIRED_COOKIE_NAMES = ["wbx-refresh", "x_wbaas_token", "_wbauid", "__wb_device_id", "wbx-validation-key"]

for p in [BASE_DIR, SESSIONS_DIR, LOG_DIR, CONFIG_DIR, CHROME_PROFILE]:
    p.mkdir(parents=True, exist_ok=True)


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def read_text(path: Path, default=""):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return default


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def get_config():
    return load_json(CONFIG_PATH, {})


def save_config(cfg):
    save_json(CONFIG_PATH, cfg)


def safe_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name)
    name = name.strip(".-_")
    if not name:
        name = "session-" + secrets.token_hex(3)
    return name[:48]


def pid_alive(pid):
    try:
        pid = int(pid)
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def kill_pid(pid):
    try:
        pid = int(pid)
        os.killpg(pid, signal.SIGTERM)
        time.sleep(0.4)
        if pid_alive(pid):
            os.killpg(pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass


def session_dir(name):
    return SESSIONS_DIR / safe_name(name)


def session_state(name):
    return load_json(session_dir(name) / "state.json", {})


def list_sessions():
    out = []
    if not SESSIONS_DIR.exists():
        return out
    for d in sorted([x for x in SESSIONS_DIR.iterdir() if x.is_dir()]):
        st = load_json(d / "state.json", {})
        pid = st.get("pid")
        link = read_text(d / "link.txt", "").strip()
        running = bool(pid and pid_alive(pid))
        out.append({
            "name": d.name,
            "pid": pid,
            "running": running,
            "link": link,
            "created_at": st.get("created_at", ""),
            "log": str(d / "log.txt"),
        })
    return out


def cookie_status():
    if not COOKIES_PATH.exists():
        return {"ok": False, "message": "Cookies не сохранены", "names": []}
    try:
        data = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
        names = [x.get("name") for x in data if isinstance(x, dict)]
        missing = [x for x in REQUIRED_COOKIE_NAMES if x not in names]
        if "__wb_device_id" not in names:
            return {"ok": False, "message": "Нет __wb_device_id", "names": names}
        if missing:
            return {"ok": True, "message": "Есть __wb_device_id, но не все ожидаемые cookies: " + ", ".join(missing), "names": names}
        return {"ok": True, "message": "Cookies OK", "names": names}
    except Exception as e:
        return {"ok": False, "message": f"Cookies JSON повреждён: {e}", "names": []}


def append_browser_log(msg):
    with BROWSER_LOG.open("a", encoding="utf-8", errors="replace") as f:
        f.write(msg.rstrip() + "\n")


def find_executable(names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def run_bg(cmd, log_file, env=None):
    with open(log_file, "a", encoding="utf-8", errors="replace") as log:
        log.write(f"CMD: {' '.join(cmd)}\n")
        log.flush()
        return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, preexec_fn=os.setsid)


def stop_browser_stack():
    st = load_json(BROWSER_STATE, {})
    for key in ["websockify", "x11vnc", "chrome", "openbox", "xvfb"]:
        pid = st.get(key)
        if pid:
            kill_pid(pid)
    save_json(BROWSER_STATE, {})


def start_browser_stack():
    BROWSER_LOG.parent.mkdir(parents=True, exist_ok=True)
    append_browser_log(f"\n=== START BROWSER {now()} ===")
    stop_browser_stack()

    xvfb = find_executable(["Xvfb"])
    openbox = find_executable(["openbox"])
    x11vnc = find_executable(["x11vnc"])
    websockify = find_executable(["websockify"])
    chrome = find_executable(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"])
    missing = [name for name, path in [("Xvfb", xvfb), ("openbox", openbox), ("x11vnc", x11vnc), ("websockify", websockify), ("Chrome/Chromium", chrome)] if not path]
    if missing:
        append_browser_log("ERROR: missing executables: " + ", ".join(missing))
        return False, "Не найдены зависимости: " + ", ".join(missing)

    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY_NUM
    env["XDG_RUNTIME_DIR"] = str(BASE_DIR / "runtime")
    Path(env["XDG_RUNTIME_DIR"]).mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(env["XDG_RUNTIME_DIR"], 0o700)
    except Exception:
        pass

    try:
        p_xvfb = run_bg([xvfb, DISPLAY_NUM, "-screen", "0", "1280x800x24", "-ac"], BROWSER_LOG, env)
        time.sleep(1.0)
        p_openbox = run_bg([openbox], BROWSER_LOG, env)
        time.sleep(0.8)
        chrome_cmd = [
            chrome,
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--window-size=1280,800",
            "--password-store=basic",
            "--use-mock-keychain",
            f"--user-data-dir={CHROME_PROFILE}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={CDP_PORT}",
            f"--app={WB_LOGIN_URL}",
        ]
        p_chrome = run_bg(chrome_cmd, BROWSER_LOG, env)
        time.sleep(2.0)
        p_x11vnc = run_bg([x11vnc, "-display", DISPLAY_NUM, "-forever", "-shared", "-nopw", "-listen", "0.0.0.0", "-rfbport", str(VNC_PORT), "-noxdamage", "-repeat"], BROWSER_LOG, env)
        time.sleep(1.0)
        p_websockify = run_bg([websockify, "--web", "/usr/share/novnc", f"0.0.0.0:{NOVNC_PORT}", f"localhost:{VNC_PORT}"], BROWSER_LOG, env)
        save_json(BROWSER_STATE, {"xvfb": p_xvfb.pid, "openbox": p_openbox.pid, "chrome": p_chrome.pid, "x11vnc": p_x11vnc.pid, "websockify": p_websockify.pid, "started_at": now()})
        append_browser_log("Browser stack started OK")
        return True, "Серверный браузер запущен"
    except Exception as e:
        append_browser_log("ERROR: " + repr(e))
        return False, repr(e)


def browser_running():
    st = load_json(BROWSER_STATE, {})
    keys = ["xvfb", "chrome", "x11vnc", "websockify"]
    return all(st.get(k) and pid_alive(st.get(k)) for k in keys)


def websocket_recv(sock):
    hdr = sock.recv(2)
    if len(hdr) < 2:
        raise RuntimeError("websocket closed")
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", sock.recv(8))[0]
    mask = sock.recv(4) if masked else b""
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if opcode == 8:
        raise RuntimeError("websocket close frame")
    return payload.decode("utf-8", errors="replace")


def websocket_send(sock, text):
    data = text.encode("utf-8")
    key = os.urandom(4)
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < (1 << 16):
        header.append(0x80 | 126)
        header += struct.pack("!H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", n)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    sock.sendall(header + key + masked)


def cdp_get_page_ws_url():
    # Wait for Chrome DevTools port.
    last = None
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=2) as r:
                arr = json.loads(r.read().decode("utf-8"))
            for item in arr:
                if item.get("type") == "page" and item.get("webSocketDebuggerUrl"):
                    return item["webSocketDebuggerUrl"]
            if arr and arr[0].get("webSocketDebuggerUrl"):
                return arr[0]["webSocketDebuggerUrl"]
        except Exception as e:
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"Chrome DevTools не отвечает: {last}")


def cdp_call(ws_url, method, params=None, timeout=5):
    u = urllib.parse.urlparse(ws_url)
    host = u.hostname or "127.0.0.1"
    port = u.port or 80
    path = u.path + (("?" + u.query) if u.query else "")
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += s.recv(4096)
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError("WebSocket handshake failed: " + resp[:200].decode(errors="replace"))
        msg_id = 1
        websocket_send(s, json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(websocket_recv(s))
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result", {})
        raise RuntimeError("CDP timeout")
    finally:
        try:
            s.close()
        except Exception:
            pass


def import_cookies_from_chrome():
    ws = cdp_get_page_ws_url()
    try:
        cdp_call(ws, "Network.enable", {}, timeout=5)
    except Exception:
        pass
    result = cdp_call(ws, "Network.getAllCookies", {}, timeout=10)
    cookies_list = result.get("cookies", [])
    filtered = []
    seen = set()
    for c in cookies_list:
        domain = c.get("domain", "") or ""
        name = c.get("name", "") or ""
        value = c.get("value", "") or ""
        if not name or not value:
            continue
        if "wb.ru" not in domain and name not in REQUIRED_COOKIE_NAMES:
            continue
        if name in seen:
            continue
        seen.add(name)
        filtered.append({"name": name, "value": value})
    # Keep required cookies first, then other wb cookies.
    filtered.sort(key=lambda x: REQUIRED_COOKIE_NAMES.index(x["name"]) if x["name"] in REQUIRED_COOKIE_NAMES else 99)
    if not filtered:
        raise RuntimeError("Не нашёл cookies wb.ru в серверном Chrome. Сначала залогинься в WB Stream в окне браузера.")
    save_json(COOKIES_PATH, filtered)
    st = cookie_status()
    if "__wb_device_id" not in st.get("names", []):
        raise RuntimeError("Cookies импортированы, но __wb_device_id не найден. Войди в WB Stream и попробуй импорт ещё раз.")
    return st


def start_session(name):
    name = safe_name(name)
    d = session_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    log_path = d / "log.txt"
    link_path = d / "link.txt"
    # Stop existing process.
    st = load_json(d / "state.json", {})
    if st.get("pid") and pid_alive(st.get("pid")):
        kill_pid(st.get("pid"))
    try:
        link_path.unlink()
    except Exception:
        pass
    if not CREATOR_BIN.exists():
        raise RuntimeError(f"Не найден {CREATOR_BIN}")
    cmd = [str(CREATOR_BIN), "--write-file", str(link_path), "--resources", "moderate"]
    if COOKIES_PATH.exists():
        cmd += ["--cookies", str(COOKIES_PATH)]
    with open(log_path, "a", encoding="utf-8", errors="replace") as log:
        log.write(f"\n=== START {now()} ===\nCMD: {' '.join(cmd)}\n")
        log.flush()
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
    save_json(d / "state.json", {"name": name, "pid": p.pid, "created_at": now(), "cmd": cmd})
    return name


def delete_session(name):
    name = safe_name(name)
    d = session_dir(name)
    st = load_json(d / "state.json", {})
    if st.get("pid") and pid_alive(st.get("pid")):
        kill_pid(st.get("pid"))
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def esc(s):
    return html.escape(str(s), quote=True)


def base_style():
    return """
<style>
:root{--bg:#0b1020;--card:#111a2e;--card2:#16213a;--text:#e9eefc;--muted:#9fb0d0;--line:#2a3657;--accent:#7c5cff;--good:#39d98a;--warn:#ffcc66;--bad:#ff6b6b;--blue:#65d5ff}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top left,#182241,#0b1020 50%);font-family:Inter,system-ui,-apple-system,Segoe UI,Arial,sans-serif;color:var(--text)}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}.wrap{max-width:1180px;margin:0 auto;padding:28px}.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}.brand{display:flex;gap:12px;align-items:center}.logo{width:42px;height:42px;border-radius:14px;background:linear-gradient(135deg,#7c5cff,#65d5ff);box-shadow:0 8px 26px rgba(124,92,255,.35)}h1{font-size:24px;margin:0}.ver{color:var(--muted);font-size:13px}.nav{display:flex;gap:10px;flex-wrap:wrap}.nav a,.btn{border:1px solid var(--line);background:rgba(255,255,255,.05);color:var(--text);padding:10px 14px;border-radius:12px;cursor:pointer;font-weight:650}.nav a:hover,.btn:hover{background:rgba(255,255,255,.09);text-decoration:none}.btn.primary{background:linear-gradient(135deg,#7c5cff,#5c8dff);border:0}.btn.danger{background:rgba(255,107,107,.12);border-color:rgba(255,107,107,.35);color:#ffd2d2}.grid{display:grid;grid-template-columns:1fr;gap:18px}.card{background:rgba(17,26,46,.86);border:1px solid var(--line);border-radius:22px;padding:20px;box-shadow:0 10px 35px rgba(0,0,0,.23)}.card h2{margin:0 0 14px;font-size:19px}.muted{color:var(--muted)}.pill{display:inline-flex;align-items:center;border-radius:999px;padding:5px 10px;font-size:12px;font-weight:700}.pill.good{background:rgba(57,217,138,.14);color:#9ff2c7}.pill.bad{background:rgba(255,107,107,.14);color:#ffc5c5}.pill.warn{background:rgba(255,204,102,.14);color:#ffe2a3}input,textarea{width:100%;background:#0c1324;border:1px solid var(--line);border-radius:12px;color:var(--text);padding:11px 12px;font:inherit}textarea{min-height:180px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.row>*{margin-top:6px}.table{width:100%;border-collapse:collapse}.table th,.table td{padding:12px;border-bottom:1px solid var(--line);vertical-align:top}.table th{text-align:left;color:var(--muted);font-size:13px}.linkbox{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#0a1020;border:1px solid var(--line);border-radius:10px;padding:9px;word-break:break-all}.flash{padding:12px 14px;border-radius:14px;background:rgba(101,213,255,.12);border:1px solid rgba(101,213,255,.25);margin-bottom:16px}.flash.err{background:rgba(255,107,107,.12);border-color:rgba(255,107,107,.25)}.login{max-width:420px;margin:12vh auto}.iframe{width:100%;height:720px;border:1px solid var(--line);border-radius:16px;background:#000}.pre{white-space:pre-wrap;background:#081022;border:1px solid var(--line);border-radius:14px;padding:14px;max-height:520px;overflow:auto;color:#cdd9f8;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}.hint{font-size:14px;line-height:1.55;color:var(--muted)}.split{display:grid;grid-template-columns:1fr 1fr;gap:18px}@media(max-width:860px){.split{grid-template-columns:1fr}.top{display:block}.nav{margin-top:14px}.iframe{height:520px}}
</style>
"""


def render_page(title, body, flash="", err=False):
    fl = f'<div class="flash {"err" if err else ""}">{esc(flash)}</div>' if flash else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>{base_style()}</head><body><div class="wrap"><div class="top"><div class="brand"><div class="logo"></div><div><h1>{APP_NAME}</h1><div class="ver">release {VERSION}</div></div></div><div class="nav"><a href="/">Ссылки</a><a href="/browser">WB Login Browser</a><a href="/settings">Настройки</a><a href="/health">Диагностика</a><a href="/logout">Выход</a></div></div>{fl}{body}</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "WLBPanel/alpha001"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def send_html(self, html_text, status=200):
        data = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, path):
        self.send_response(302)
        self.send_header("Location", path)
        self.end_headers()

    def parse_post(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n).decode("utf-8", errors="replace")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def is_authed(self):
        cfg = get_config()
        token = cfg.get("session_token")
        if not token:
            return False
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        got = c.get("wlb_session")
        return bool(got and hmac.compare_digest(got.value, token))

    def require_auth(self):
        if not self.is_authed():
            self.redirect("/login")
            return False
        return True

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/login": return self.login_get()
        if p == "/logout": return self.logout()
        if not self.require_auth(): return
        if p == "/": return self.index()
        if p == "/settings": return self.settings()
        if p == "/browser": return self.browser()
        if p == "/health": return self.health()
        if p.startswith("/logs/"): return self.logs(p.split("/",2)[2])
        if p == "/browser-log": return self.browser_log()
        self.send_error(404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/login": return self.login_post()
        if not self.require_auth(): return
        try:
            if p == "/create": return self.create_post()
            if p == "/delete": return self.delete_post()
            if p == "/save-cookies": return self.save_cookies_post()
            if p == "/change-password": return self.change_password_post()
            if p == "/start-browser": return self.start_browser_post()
            if p == "/stop-browser": return self.stop_browser_post()
            if p == "/import-cookies": return self.import_cookies_post()
        except Exception as e:
            return self.send_html(render_page("Ошибка", f"<div class='card'><h2>Ошибка</h2><div class='pre'>{esc(repr(e))}</div><p><a class='btn' href='/'>Назад</a></p></div>", err=True), 500)
        self.send_error(404)

    def login_get(self):
        body = """<div class="login card"><h2>Вход</h2><form method="post" action="/login"><p><input name="username" placeholder="Логин" autofocus></p><p><input name="password" type="password" placeholder="Пароль"></p><p><button class="btn primary" type="submit">Войти</button></p></form><p class="hint">После входа сразу смени временный пароль в Настройках.</p></div>"""
        self.send_html(f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Login</title>{base_style()}</head><body>{body}</body></html>")

    def login_post(self):
        form = self.parse_post()
        u = form.get("username", [""])[0]
        p = form.get("password", [""])[0]
        cfg = get_config()
        if u == cfg.get("username", "admin") and hmac.compare_digest(sha256_hex(p), cfg.get("password_sha256", "")):
            token = secrets.token_urlsafe(32)
            cfg["session_token"] = token
            save_config(cfg)
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"wlb_session={token}; HttpOnly; Path=/; SameSite=Lax")
            self.end_headers()
        else:
            self.send_html(render_page("Login", "<div class='card'><h2>Неверный логин или пароль</h2><p><a class='btn' href='/login'>Повторить</a></p></div>", err=True), 403)

    def logout(self):
        cfg = get_config(); cfg.pop("session_token", None); save_config(cfg)
        self.send_response(302); self.send_header("Location", "/login"); self.send_header("Set-Cookie", "wlb_session=deleted; Max-Age=0; Path=/"); self.end_headers()

    def index(self, flash=""):
        cs = cookie_status()
        pill = "good" if cs["ok"] else "bad"
        rows = []
        for s in list_sessions():
            status = "active" if s["running"] else "stopped"
            status_cls = "good" if s["running"] else "bad"
            link = f"<div class='linkbox'>{esc(s['link'])}</div>" if s["link"] else "<span class='muted'>ссылка ещё создаётся...</span>"
            rows.append(f"<tr><td><b>{esc(s['name'])}</b><br><span class='muted'>PID: {esc(s['pid'] or '-')}</span></td><td><span class='pill {status_cls}'>{status}</span></td><td>{link}</td><td><div class='row'><a class='btn' href='/logs/{esc(s['name'])}'>Логи</a><form method='post' action='/delete' onsubmit='return confirm(\"Удалить ссылку?\")'><input type='hidden' name='name' value='{esc(s['name'])}'><button class='btn danger'>Удалить</button></form></div></td></tr>")
        if not rows:
            rows.append("<tr><td colspan='4' class='muted'>Пока нет ссылок.</td></tr>")
        body = f"""
<div class="grid">
<div class="card"><h2>Статус</h2><div class="row"><span class="pill {pill}">{esc(cs['message'])}</span><span class="muted">Cookies: {esc(', '.join(cs.get('names', [])) or 'нет')}</span></div><p class="hint">Сначала открой WB Login Browser, залогинься в WB Stream, нажми “Импортировать cookies”, затем создавай ссылки. Созданные ссылки держатся процессами на сервере и не закрываются, когда ты закрываешь страницу панели.</p></div>
<div class="card"><h2>Создать WB Stream ссылку</h2><form method="post" action="/create"><div class="row"><input style="max-width:360px" name="name" placeholder="Имя ссылки, например phone1"><button class="btn primary" type="submit">Создать ссылку</button></div></form></div>
<div class="card"><h2>Ссылки</h2><table class="table"><thead><tr><th>Имя</th><th>Статус</th><th>Join link</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</div>"""
        self.send_html(render_page("Ссылки", body, flash))

    def create_post(self):
        form = self.parse_post(); name = form.get("name", [""])[0]
        name = start_session(name)
        self.index(f"Сессия {name} создана. Ссылка появится через 10–40 секунд.")

    def delete_post(self):
        form = self.parse_post(); name = form.get("name", [""])[0]
        delete_session(name)
        self.index(f"Сессия {safe_name(name)} удалена.")

    def settings(self, flash=""):
        cs = cookie_status()
        cookies_text = read_text(COOKIES_PATH, "")
        body = f"""
<div class="split">
<div class="card"><h2>Сменить пароль</h2><form method="post" action="/change-password"><p><input type="password" name="old" placeholder="Текущий пароль"></p><p><input type="password" name="new1" placeholder="Новый пароль, минимум 8 символов"></p><p><input type="password" name="new2" placeholder="Повтори новый пароль"></p><button class="btn primary">Сменить пароль</button></form></div>
<div class="card"><h2>WB Stream cookies</h2><p><span class="pill {'good' if cs['ok'] else 'bad'}">{esc(cs['message'])}</span></p><form method="post" action="/save-cookies"><textarea name="cookies_json" spellcheck="false">{esc(cookies_text)}</textarea><p><button class="btn primary">Сохранить cookies</button></p></form><p class="hint">Ожидаются cookies вида: wbx-refresh, x_wbaas_token, _wbauid, __wb_device_id, wbx-validation-key. Лучше импортировать автоматически из WB Login Browser.</p></div>
</div>"""
        self.send_html(render_page("Настройки", body, flash))

    def save_cookies_post(self):
        form = self.parse_post(); txt = form.get("cookies_json", [""])[0].strip()
        if not txt:
            try: COOKIES_PATH.unlink()
            except Exception: pass
            return self.settings("Cookies очищены")
        data = json.loads(txt)
        if not isinstance(data, list):
            raise RuntimeError("Cookies должны быть JSON-массивом")
        clean = []
        for x in data:
            if isinstance(x, dict) and x.get("name") and x.get("value"):
                clean.append({"name": x["name"], "value": x["value"]})
        save_json(COOKIES_PATH, clean)
        self.settings("Cookies сохранены")

    def change_password_post(self):
        form = self.parse_post()
        old = form.get("old", [""])[0]; n1 = form.get("new1", [""])[0]; n2 = form.get("new2", [""])[0]
        cfg = get_config()
        if not hmac.compare_digest(sha256_hex(old), cfg.get("password_sha256", "")):
            raise RuntimeError("Текущий пароль неверный")
        if n1 != n2:
            raise RuntimeError("Новые пароли не совпадают")
        if len(n1) < 8:
            raise RuntimeError("Пароль должен быть минимум 8 символов")
        cfg["password_sha256"] = sha256_hex(n1); save_config(cfg)
        self.settings("Пароль изменён")

    def browser(self, flash=""):
        running = browser_running()
        scheme = "http"
        host = self.headers.get("Host", "").split(":")[0] or "127.0.0.1"
        novnc_url = f"{scheme}://{host}:{NOVNC_PORT}/vnc.html?autoconnect=true&resize=scale"
        cs = cookie_status()
        body = f"""
<div class="grid">
<div class="card"><h2>WB Login Browser</h2><p class="hint">Это серверный Chrome. Закрытие твоей страницы в браузере не закрывает уже созданные WB Stream ссылки. После входа нажми “Импортировать cookies из серверного Chrome”.</p><div class="row"><form method="post" action="/start-browser"><button class="btn primary">Запустить серверный браузер</button></form><form method="post" action="/import-cookies"><button class="btn">Импортировать cookies из серверного Chrome</button></form><form method="post" action="/stop-browser"><button class="btn danger">Остановить браузер</button></form><a class="btn" target="_blank" href="{esc(novnc_url)}">Открыть noVNC в новой вкладке</a></div><p>Статус браузера: <span class="pill {'good' if running else 'warn'}">{'запущен' if running else 'не запущен'}</span> &nbsp; Cookies: <span class="pill {'good' if cs['ok'] else 'bad'}">{esc(cs['message'])}</span></p></div>
<div class="card"><h2>Окно серверного браузера</h2><iframe class="iframe" src="{esc(novnc_url) if running else 'about:blank'}"></iframe></div>
<div class="card"><h2>Логи браузера</h2><p><a class="btn" href="/browser-log">Открыть полный лог</a></p><div class="pre">{esc(read_text(BROWSER_LOG, '')[-5000:])}</div></div>
</div>"""
        self.send_html(render_page("WB Login Browser", body, flash))

    def start_browser_post(self):
        ok, msg = start_browser_stack()
        self.browser(msg)

    def stop_browser_post(self):
        stop_browser_stack()
        self.browser("Серверный браузер остановлен")

    def import_cookies_post(self):
        st = import_cookies_from_chrome()
        self.browser("Cookies импортированы: " + st["message"])

    def logs(self, name):
        name = safe_name(name)
        p = session_dir(name) / "log.txt"
        body = f"<div class='card'><h2>Логи: {esc(name)}</h2><div class='pre'>{esc(read_text(p, 'Лог пустой'))}</div><p><a class='btn' href='/'>Назад</a></p></div>"
        self.send_html(render_page("Логи", body))

    def browser_log(self):
        body = f"<div class='card'><h2>Логи браузера</h2><div class='pre'>{esc(read_text(BROWSER_LOG, 'Лог пустой'))}</div><p><a class='btn' href='/browser'>Назад</a></p></div>"
        self.send_html(render_page("Логи браузера", body))

    def health(self):
        checks = []
        for label, names in [("Xvfb", ["Xvfb"]), ("openbox", ["openbox"]), ("x11vnc", ["x11vnc"]), ("websockify", ["websockify"]), ("Chrome", ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]), ("headless-wbstream-creator", [str(CREATOR_BIN)])]:
            if label == "headless-wbstream-creator":
                ok = CREATOR_BIN.exists() and os.access(CREATOR_BIN, os.X_OK)
                val = str(CREATOR_BIN) if ok else "not found"
            else:
                val = find_executable(names)
                ok = bool(val)
            checks.append((label, ok, val or "not found"))
        rows = "".join([f"<tr><td>{esc(a)}</td><td><span class='pill {'good' if b else 'bad'}'>{'OK' if b else 'FAIL'}</span></td><td>{esc(c)}</td></tr>" for a,b,c in checks])
        body = f"<div class='card'><h2>Диагностика</h2><table class='table'><tbody>{rows}</tbody></table><p>Browser running: <span class='pill {'good' if browser_running() else 'warn'}'>{browser_running()}</span></p><p>Cookies: {esc(cookie_status()['message'])}</p></div>"
        self.send_html(render_page("Диагностика", body))


def main():
    cfg = get_config()
    if not cfg.get("username") or not cfg.get("password_sha256"):
        pw = secrets.token_urlsafe(12)
        cfg = {"username": "admin", "password_sha256": sha256_hex(pw)}
        save_config(cfg)
        print("Generated password:", pw)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"{APP_NAME} {VERSION} listening on {HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
