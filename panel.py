#!/usr/bin/env python3
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote

APP_NAME = "WLB Panel v3"
CONFIG_PATH = Path("/etc/wlb-panel/config.json")
STATE_PATH = Path("/var/lib/wlb-panel/state.json")
SESSIONS_DIR = Path("/var/lib/wlb-panel/sessions")
LOG_DIR = Path("/var/log/wlb-panel")
COOKIES_PATH = Path("/etc/wlb-panel/wb-cookies.json")
BIN_PATH = Path("/opt/whitelist-bypass/headless-wbstream-creator")
CHROME_PROFILE = Path("/var/lib/wlb-panel/chrome-profile")
BROWSER_STATE = Path("/var/lib/wlb-panel/browser.json")
PORT = int(os.environ.get("WLB_PANEL_PORT", "8088"))


def ensure_dirs():
    for p in [SESSIONS_DIR, LOG_DIR, COOKIES_PATH.parent, CHROME_PROFILE]:
        p.mkdir(parents=True, exist_ok=True)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def cfg():
    return load_json(CONFIG_PATH, {})


def state():
    return load_json(STATE_PATH, {"sessions": {}})


def save_state(st):
    save_json(STATE_PATH, st)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def check_auth(headers):
    cookie = headers.get("Cookie", "")
    m = re.search(r"wlb_session=([A-Za-z0-9_\-]+)", cookie)
    if not m:
        return False
    token = m.group(1)
    c = cfg()
    expected = c.get("session_token", "")
    return expected and hmac.compare_digest(token, expected)


def redirect(handler, location):
    handler.send_response(302)
    handler.send_header("Location", location)
    handler.end_headers()


def read_body(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    return handler.rfile.read(length).decode("utf-8", errors="replace")


def sanitize_name(name):
    name = name.strip()
    name = re.sub(r"[^A-Za-z0-9_.\-а-яА-ЯёЁ]+", "_", name)
    return name[:64]


def is_pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def kill_pid(pid):
    try:
        os.kill(int(pid), signal.SIGTERM)
        time.sleep(0.7)
        if is_pid_alive(pid):
            os.kill(int(pid), signal.SIGKILL)
    except Exception:
        pass


def cookies_status():
    if not COOKIES_PATH.exists():
        return False, "cookies не сохранены"
    try:
        data = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
        names = {c.get("name") for c in data if isinstance(c, dict)}
        if "__wb_device_id" not in names:
            return False, "нет __wb_device_id"
        return True, "OK, __wb_device_id найден"
    except Exception as e:
        return False, f"JSON ошибка: {e}"


def start_session(name, resources="moderate"):
    ensure_dirs()
    name = sanitize_name(name)
    if not name:
        raise ValueError("Пустое имя")
    st = state()
    sessions = st.setdefault("sessions", {})
    if name in sessions and sessions[name].get("pid") and is_pid_alive(sessions[name]["pid"]):
        raise ValueError("Сессия с таким именем уже запущена")
    ok, msg = cookies_status()
    if not ok:
        raise ValueError("WB cookies не готовы: " + msg)
    sdir = SESSIONS_DIR / name
    sdir.mkdir(parents=True, exist_ok=True)
    link_file = sdir / "link.txt"
    log_file = LOG_DIR / f"{name}.log"
    try:
        link_file.unlink()
    except FileNotFoundError:
        pass
    cmd = [str(BIN_PATH), "--write-file", str(link_file), "--resources", resources, "--cookies", str(COOKIES_PATH)]
    with open(log_file, "ab", buffering=0) as lf:
        lf.write((f"\n=== START {time.strftime('%Y-%m-%d %H:%M:%S')} ===\nCMD: {' '.join(cmd)}\n").encode())
        p = subprocess.Popen(cmd, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True)
    sessions[name] = {"pid": p.pid, "created_at": int(time.time()), "link_file": str(link_file), "log_file": str(log_file), "resources": resources}
    save_state(st)
    return p.pid


def delete_session(name):
    name = sanitize_name(name)
    st = state()
    sess = st.get("sessions", {}).get(name)
    if sess:
        pid = sess.get("pid")
        if pid:
            kill_pid(pid)
        st.get("sessions", {}).pop(name, None)
        save_state(st)
    shutil.rmtree(SESSIONS_DIR / name, ignore_errors=True)


def get_sessions_view():
    st = state()
    out = []
    changed = False
    for name, sess in list(st.get("sessions", {}).items()):
        pid = sess.get("pid")
        active = bool(pid and is_pid_alive(pid))
        link = ""
        lf = Path(sess.get("link_file", ""))
        if lf.exists():
            link = lf.read_text(encoding="utf-8", errors="replace").strip()
        out.append({"name": name, "pid": pid, "active": active, "link": link, "created_at": sess.get("created_at", 0), "log_file": sess.get("log_file", "")})
        if not active and sess.get("active"):
            changed = True
    if changed:
        save_state(st)
    return out


def browser_state():
    return load_json(BROWSER_STATE, {})


def save_browser_state(data):
    save_json(BROWSER_STATE, data)


def find_chromium():
    for x in ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/snap/bin/chromium", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]:
        if Path(x).exists():
            return x
    return None


def start_browser_stack():
    ensure_dirs()
    bs = browser_state()
    # if existing websockify alive, reuse
    if bs.get("websockify_pid") and is_pid_alive(bs["websockify_pid"]):
        return
    stop_browser_stack()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / "browser.log", "ab", buffering=0)
    display = ":99"
    xvfb = subprocess.Popen(["Xvfb", display, "-screen", "0", "1280x900x24", "-nolisten", "tcp"], stdout=log, stderr=log, start_new_session=True)
    time.sleep(1)
    env = os.environ.copy(); env["DISPLAY"] = display
    wm_bin = shutil.which("openbox") or shutil.which("fluxbox") or "true"
    wm = subprocess.Popen([wm_bin], stdout=log, stderr=log, env=env, start_new_session=True)
    x11vnc = subprocess.Popen(["x11vnc", "-display", display, "-rfbport", "5901", "-forever", "-shared", "-nopw", "-quiet"], stdout=log, stderr=log, env=env, start_new_session=True)
    time.sleep(1)
    # websockify command may be module or binary
    web_cmd = ["websockify", "--web", "/usr/share/novnc", "6080", "127.0.0.1:5901"]
    websockify = subprocess.Popen(web_cmd, stdout=log, stderr=log, start_new_session=True)
    chrome = find_chromium()
    if not chrome:
        raise RuntimeError("Chromium не найден. Установи chromium/chromium-browser")
    chrome_args = [chrome, "--no-sandbox", "--disable-dev-shm-usage", "--password-store=basic", "--user-data-dir=" + str(CHROME_PROFILE), "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=9222", "--window-size=1280,900", "https://stream.wb.ru"]
    ch = subprocess.Popen(chrome_args, stdout=log, stderr=log, env=env, start_new_session=True)
    save_browser_state({"xvfb_pid": xvfb.pid, "wm_pid": wm.pid, "x11vnc_pid": x11vnc.pid, "websockify_pid": websockify.pid, "chromium_pid": ch.pid, "started_at": int(time.time())})


def stop_browser_stack():
    bs = browser_state()
    for key in ["chromium_pid", "websockify_pid", "x11vnc_pid", "wm_pid", "xvfb_pid"]:
        if bs.get(key):
            kill_pid(bs[key])
    save_browser_state({})


def http_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ws_frame(data):
    payload = data.encode()
    header = bytearray([0x81])
    n = len(payload)
    mask_bit = 0x80
    if n < 126:
        header.append(mask_bit | n)
    elif n < 65536:
        header.append(mask_bit | 126); header += struct.pack("!H", n)
    else:
        header.append(mask_bit | 127); header += struct.pack("!Q", n)
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + mask + masked


def ws_read(sock):
    b1, b2 = sock.recv(2)
    length = b2 & 0x7f
    if length == 126:
        length = struct.unpack("!H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", sock.recv(8))[0]
    masked = b2 & 0x80
    mask = sock.recv(4) if masked else b""
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk: break
        data += chunk
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return data.decode(errors="replace")


def cdp_call(ws_url, method, params=None, msg_id=1):
    # ws://127.0.0.1:9222/devtools/page/...
    u = urlparse(ws_url)
    host, port = u.hostname, u.port or 80
    path = u.path + (("?" + u.query) if u.query else "")
    key = base64.b64encode(os.urandom(16)).decode()
    s = socket.create_connection((host, port), timeout=5)
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    s.sendall(req.encode())
    resp = s.recv(4096)
    if b"101" not in resp.split(b"\r\n", 1)[0]:
        raise RuntimeError("CDP websocket upgrade failed")
    payload = json.dumps({"id": msg_id, "method": method, "params": params or {}})
    s.sendall(ws_frame(payload))
    deadline = time.time() + 10
    while time.time() < deadline:
        msg = ws_read(s)
        try:
            data = json.loads(msg)
        except Exception:
            continue
        if data.get("id") == msg_id:
            s.close()
            if "error" in data:
                raise RuntimeError(str(data["error"]))
            return data.get("result", {})
    s.close()
    raise RuntimeError("CDP timeout")


def import_cookies_from_chromium():
    # open a page target and get all cookies
    targets = http_json("http://127.0.0.1:9222/json", timeout=5)
    ws_url = None
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            ws_url = t["webSocketDebuggerUrl"]
            break
    if not ws_url:
        raise RuntimeError("Не найден CDP page target. Открой серверный браузер.")
    res = cdp_call(ws_url, "Network.getAllCookies", {}, 1)
    cookies = res.get("cookies", [])
    wb = []
    for c in cookies:
        d = c.get("domain", "")
        if "wb.ru" in d or "wildberries" in d or "wbstream" in d:
            wb.append({k: c.get(k) for k in ["name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"] if k in c})
    if not wb:
        raise RuntimeError("WB cookies не найдены. Залогинься в WB Stream в серверном браузере.")
    COOKIES_PATH.write_text(json.dumps(wb, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(COOKIES_PATH, 0o600)
    except Exception:
        pass
    ok, msg = cookies_status()
    return len(wb), ok, msg


def page(title, body, handler=None):
    nav = '<a href="/">Ссылки</a> · <a href="/settings">Настройки</a> · <a href="/browser">WB Login Browser</a> · <a href="/logout">Выход</a>'
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{html.escape(title)}</title><style>
body{{font-family:Arial,sans-serif;margin:24px;background:#f6f7fb;color:#111}} .box{{background:#fff;border-radius:12px;padding:18px;margin:0 0 16px;box-shadow:0 1px 4px #0001}} input,textarea,button,select{{font-size:15px;padding:9px;border:1px solid #ccc;border-radius:8px}} textarea{{width:100%;min-height:180px;font-family:monospace}} button{{cursor:pointer;background:#111;color:#fff;border:0}} .danger{{background:#b00020}} .muted{{color:#666}} code{{background:#eee;padding:2px 5px;border-radius:4px}} table{{border-collapse:collapse;width:100%}} td,th{{padding:8px;border-bottom:1px solid #eee;text-align:left}} .ok{{color:green}} .bad{{color:#b00020}} .link{{word-break:break-all;font-family:monospace}}</style></head><body><div class='box'><b>{APP_NAME}</b><br>{nav}</div>{body}</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "WLBPanel/3"

    def send_html(self, s, code=200):
        b = s.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def require_auth(self):
        if not check_auth(self.headers):
            redirect(self, "/login"); return False
        return True

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/login": return self.login_page()
        if p == "/logout":
            self.send_response(302); self.send_header("Set-Cookie", "wlb_session=; Max-Age=0; Path=/"); self.send_header("Location", "/login"); self.end_headers(); return
        if not self.require_auth(): return
        if p == "/": return self.index()
        if p == "/settings": return self.settings()
        if p == "/browser": return self.browser()
        if p == "/logs": return self.logs()
        self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/login": return self.login_post()
        if not self.require_auth(): return
        if p == "/create": return self.create_post()
        if p == "/delete": return self.delete_post()
        if p == "/save-cookies": return self.save_cookies_post()
        if p == "/change-password": return self.change_password_post()
        if p == "/browser-start":
            try:
                start_browser_stack(); redirect(self, "/browser?msg=started")
            except Exception as e:
                redirect(self, "/browser?err=" + quote(str(e)))
            return
        if p == "/browser-stop":
            stop_browser_stack(); redirect(self, "/browser?msg=stopped"); return
        if p == "/browser-import-cookies":
            try:
                n, ok, msg = import_cookies_from_chromium(); redirect(self, "/browser?msg=" + quote(f"Импортировано cookies: {n}. {msg}"))
            except Exception as e:
                redirect(self, "/browser?err=" + quote(str(e)))
            return
        self.send_error(404)

    def login_page(self, err=""):
        body = f"<div class='box'><h2>Вход</h2>{'<p class=bad>'+html.escape(err)+'</p>' if err else ''}<form method=post><p><input name=username placeholder='login'></p><p><input name=password type=password placeholder='password'></p><button>Войти</button></form></div>"
        self.send_html(page("Login", body))

    def login_post(self):
        data = parse_qs(read_body(self))
        c = cfg(); u = data.get("username", [""])[0]; pw = data.get("password", [""])[0]
        if hmac.compare_digest(u, c.get("username", "admin")) and hmac.compare_digest(sha256(pw), c.get("password_sha256", "")):
            token = secrets.token_urlsafe(32); c["session_token"] = token; save_json(CONFIG_PATH, c)
            self.send_response(302); self.send_header("Set-Cookie", f"wlb_session={token}; HttpOnly; Path=/; SameSite=Lax"); self.send_header("Location", "/"); self.end_headers(); return
        self.login_page("Неверный логин или пароль")

    def index(self):
        ok, msg = cookies_status()
        rows = ""
        for s in get_sessions_view():
            link = html.escape(s["link"]) if s["link"] else "<span class='muted'>ссылка ещё создаётся...</span>"
            status = "active" if s["active"] else "stopped"
            rows += f"<tr><td>{html.escape(s['name'])}</td><td>{status}</td><td>{s['pid']}</td><td class='link'>{link}</td><td><a href='/logs?name={quote(s['name'])}'>Логи</a></td><td><form method=post action='/delete'><input type=hidden name=name value='{html.escape(s['name'])}'><button class=danger>Удалить</button></form></td></tr>"
        body = f"""<div class='box'><h2>Создать WB Stream ссылку</h2><p>Cookies: <b class={'ok' if ok else 'bad'}>{html.escape(msg)}</b></p><form method=post action='/create'><input name=name placeholder='Имя ссылки, например phone1'> <select name=resources><option>moderate</option><option>default</option><option>unlimited</option></select> <button>Создать ссылку</button></form></div><div class='box'><h2>Ссылки</h2><table><tr><th>Имя</th><th>Статус</th><th>PID</th><th>Ссылка</th><th>Логи</th><th></th></tr>{rows}</table></div>"""
        self.send_html(page("Links", body))

    def create_post(self):
        data = parse_qs(read_body(self)); name = data.get("name", [""])[0]; res = data.get("resources", ["moderate"])[0]
        try:
            start_session(name, res)
            redirect(self, "/")
        except Exception as e:
            self.send_html(page("Error", f"<div class='box'><p class='bad'>{html.escape(str(e))}</p><p><a href='/'>Назад</a></p></div>"), 400)

    def delete_post(self):
        data = parse_qs(read_body(self)); delete_session(data.get("name", [""])[0]); redirect(self, "/")

    def settings(self):
        ok, msg = cookies_status(); cookies_text = COOKIES_PATH.read_text(encoding="utf-8", errors="replace") if COOKIES_PATH.exists() else ""
        body = f"""<div class='box'><h2>Сменить пароль</h2><form method=post action='/change-password'><p><input type=password name=old placeholder='Старый пароль'></p><p><input type=password name=new1 placeholder='Новый пароль'></p><p><input type=password name=new2 placeholder='Повтори новый пароль'></p><button>Сменить пароль</button></form></div><div class='box'><h2>WB Stream cookies</h2><p>Статус: <b class={'ok' if ok else 'bad'}>{html.escape(msg)}</b></p><form method=post action='/save-cookies'><textarea name=cookies>{html.escape(cookies_text)}</textarea><p><button>Сохранить cookies</button></p></form></div>"""
        self.send_html(page("Settings", body))

    def save_cookies_post(self):
        data = parse_qs(read_body(self)); txt = data.get("cookies", [""])[0].strip()
        try:
            parsed = json.loads(txt)
            if not isinstance(parsed, list): raise ValueError("cookies должны быть JSON-массивом")
            COOKIES_PATH.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(COOKIES_PATH, 0o600)
            redirect(self, "/settings")
        except Exception as e:
            self.send_html(page("Error", f"<div class='box'><p class='bad'>{html.escape(str(e))}</p><p><a href='/settings'>Назад</a></p></div>"), 400)

    def change_password_post(self):
        data = parse_qs(read_body(self)); old = data.get("old", [""])[0]; n1 = data.get("new1", [""])[0]; n2 = data.get("new2", [""])[0]
        c = cfg()
        if not hmac.compare_digest(sha256(old), c.get("password_sha256", "")):
            return self.send_html(page("Error", "<div class='box'><p class='bad'>Старый пароль неверный</p><p><a href='/settings'>Назад</a></p></div>"), 400)
        if n1 != n2 or len(n1) < 8:
            return self.send_html(page("Error", "<div class='box'><p class='bad'>Новые пароли не совпадают или меньше 8 символов</p><p><a href='/settings'>Назад</a></p></div>"), 400)
        c["password_sha256"] = sha256(n1); save_json(CONFIG_PATH, c); redirect(self, "/settings")

    def browser(self):
        qs = parse_qs(urlparse(self.path).query); msg = qs.get("msg", [""])[0]; err = qs.get("err", [""])[0]
        bs = browser_state(); running = bool(bs.get("websockify_pid") and is_pid_alive(bs["websockify_pid"]))
        host = self.headers.get("Host", "").split(":")[0] or "SERVER_IP"
        novnc_url = f"http://{host}:6080/vnc.html?autoconnect=true&resize=scale"
        ok, cmsg = cookies_status()
        frame = f"<iframe src='{html.escape(novnc_url)}' style='width:100%;height:720px;border:1px solid #ccc;border-radius:8px'></iframe>" if running else "<p class='muted'>Браузер не запущен</p>"
        body = f"""<div class='box'><h2>WB Login Browser на сервере</h2>{'<p class=ok>'+html.escape(msg)+'</p>' if msg else ''}{'<p class=bad>'+html.escape(err)+'</p>' if err else ''}<p>Статус браузера: <b>{'running' if running else 'stopped'}</b></p><p>Cookies: <b class={'ok' if ok else 'bad'}>{html.escape(cmsg)}</b></p><form method=post action='/browser-start' style='display:inline'><button>Запустить серверный браузер</button></form> <form method=post action='/browser-stop' style='display:inline'><button class=danger>Остановить</button></form> <form method=post action='/browser-import-cookies' style='display:inline'><button>Импортировать cookies из браузера</button></form><p class='muted'>Открой WB Stream в окне ниже, залогинься, затем нажми “Импортировать cookies”. Если окно не открывается, проверь порт 6080 у firewall.</p></div><div class='box'>{frame}</div>"""
        self.send_html(page("WB Browser", body))

    def logs(self):
        name = sanitize_name(parse_qs(urlparse(self.path).query).get("name", [""])[0])
        lf = LOG_DIR / f"{name}.log"
        text = lf.read_text(encoding="utf-8", errors="replace")[-20000:] if name and lf.exists() else "Нет лога"
        self.send_html(page("Logs", f"<div class='box'><h2>Логи: {html.escape(name)}</h2><pre style='white-space:pre-wrap'>{html.escape(text)}</pre><p><a href='/'>Назад</a></p></div>"))


def main():
    ensure_dirs()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"{APP_NAME} listening on 0.0.0.0:{PORT}")
    httpd.serve_forever()

if __name__ == "__main__":
    main()
