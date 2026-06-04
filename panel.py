#!/usr/bin/env python3
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import signal
import subprocess
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("WLB_PANEL_PORT", "8088"))
CONFIG_PATH = os.environ.get("WLB_PANEL_CONFIG", "/etc/wlb-panel/config.json")
DATA_DIR = os.environ.get("WLB_PANEL_DATA", "/var/lib/wlb-panel")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
LOG_DIR = os.environ.get("WLB_PANEL_LOG", "/var/log/wlb-panel")
CREATOR_BIN = os.environ.get("WLB_CREATOR_BIN", "/opt/whitelist-bypass/headless-wbstream-creator")
COOKIES_PATH = os.environ.get("WLB_COOKIES", "/etc/wlb-panel/wb-cookies.json")
RESOURCES = os.environ.get("WLB_RESOURCES", "moderate")

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)

SESSION_TOKENS = set()
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def pbkdf2_hash(password: str, salt: str | None = None) -> dict:
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return {"scheme": "pbkdf2_sha256", "salt": salt, "hash": dk.hex()}


def verify_password(password: str, cfg: dict) -> bool:
    # v2 format
    if cfg.get("password", {}).get("scheme") == "pbkdf2_sha256":
        p = cfg["password"]
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(p["salt"]), 200_000)
        return hmac.compare_digest(dk.hex(), p.get("hash", ""))
    # v1 compatibility
    if "password_sha256" in cfg:
        return hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), cfg.get("password_sha256", ""))
    return False


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        cfg = {"username": "admin", "password": pbkdf2_hash(secrets.token_urlsafe(12))}
        save_config(cfg)
        return cfg
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


def set_password(new_password: str) -> None:
    cfg = load_config()
    cfg["password"] = pbkdf2_hash(new_password)
    cfg.pop("password_sha256", None)
    save_config(cfg)


def is_authenticated(handler) -> bool:
    raw = handler.headers.get("Cookie", "")
    try:
        c = cookies.SimpleCookie(raw)
        token = c.get("wlb_session")
        return bool(token and token.value in SESSION_TOKENS)
    except Exception:
        return False


def html_page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:Arial,Helvetica,sans-serif;margin:0;background:#f5f6f8;color:#111}}
header{{display:flex;justify-content:space-between;align-items:center;padding:24px 32px;background:#fff;border-bottom:1px solid #ddd}}
main{{padding:24px;max-width:1280px;margin:0 auto}}
.card{{background:#fff;border:1px solid #ddd;border-radius:12px;padding:20px;margin-bottom:18px;box-shadow:0 1px 4px #0001}}
input,textarea,select{{padding:10px;border:1px solid #ccc;border-radius:8px;font-size:14px}}
textarea{{width:100%;min-height:160px;font-family:monospace;box-sizing:border-box}}
button,.btn{{background:#111;color:#fff;border:0;border-radius:8px;padding:10px 14px;text-decoration:none;cursor:pointer;display:inline-block}}
button.danger,.danger{{background:#b00020}}
button.secondary,.secondary{{background:#555}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:10px;border-bottom:1px solid #eee;vertical-align:top}}
small,.muted{{color:#666}}
code{{background:#eee;padding:6px 8px;border-radius:6px;display:inline-block;max-width:100%;overflow:auto}}
.notice{{padding:12px;border-radius:8px;margin-bottom:12px}}
.warn{{background:#fff3cd;border:1px solid #ffe69c}}
.ok{{background:#d1e7dd;border:1px solid #a3cfbb}}
.err{{background:#f8d7da;border:1px solid #f1aeb5}}
.topnav a{{margin-left:12px;color:#111}}
.copybox{{display:flex;gap:8px;align-items:center}}
.copybox input{{flex:1;font-family:monospace}}
</style>
</head>
<body>
<header>
  <div><h1 style="margin:0">Whitelist Bypass Panel</h1><small>WB Stream only · порт {PORT}</small></div>
  <nav class="topnav"><a href="/">Главная</a><a href="/settings">Настройки</a><a href="/logout">Выйти</a></nav>
</header>
<main>{body}</main>
</body></html>""".encode("utf-8")


def redirect(handler, path="/"):
    handler.send_response(302)
    handler.send_header("Location", path)
    handler.end_headers()


def read_post(handler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    body = handler.rfile.read(length).decode("utf-8", errors="replace")
    return {k: v[0] if v else "" for k, v in parse_qs(body).items()}


def session_dir(name: str) -> str:
    return os.path.join(SESSIONS_DIR, name)


def session_meta_path(name: str) -> str:
    return os.path.join(session_dir(name), "meta.json")


def load_meta(name: str) -> dict:
    try:
        with open(session_meta_path(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_meta(name: str, meta: dict) -> None:
    os.makedirs(session_dir(name), exist_ok=True)
    with open(session_meta_path(name), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def list_sessions() -> list[str]:
    if not os.path.isdir(SESSIONS_DIR):
        return []
    return sorted([x for x in os.listdir(SESSIONS_DIR) if NAME_RE.match(x) and os.path.isdir(session_dir(x))])


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_link(name: str) -> str:
    path = os.path.join(session_dir(name), "link.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def read_log(name: str, max_bytes: int = 40_000) -> str:
    path = os.path.join(LOG_DIR, f"{name}.log")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            return f.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"Лог недоступен: {e}"


def cookies_ready() -> bool:
    return os.path.exists(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 5


def start_session(name: str) -> tuple[bool, str]:
    if not NAME_RE.match(name):
        return False, "Имя должно содержать только латиницу, цифры, точку, дефис или подчёркивание; максимум 40 символов."
    if not os.path.exists(CREATOR_BIN):
        return False, f"Не найден бинарник: {CREATOR_BIN}"
    if load_meta(name).get("pid") and pid_alive(int(load_meta(name)["pid"])):
        return False, "Сессия с таким именем уже запущена."

    os.makedirs(session_dir(name), exist_ok=True)
    link_path = os.path.join(session_dir(name), "link.txt")
    log_path = os.path.join(LOG_DIR, f"{name}.log")
    try:
        if os.path.exists(link_path):
            os.remove(link_path)
    except Exception:
        pass

    cmd = [CREATOR_BIN, "--write-file", link_path, "--resources", RESOURCES]
    if cookies_ready():
        cmd += ["--cookies", COOKIES_PATH]

    logf = open(log_path, "ab", buffering=0)
    logf.write(("\n\n=== START %s ===\nCMD: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(cmd))).encode())
    if not cookies_ready():
        logf.write(b"WARNING: cookies file is not configured. Creator may fail with '--cookies is required'.\n")

    try:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, preexec_fn=os.setsid)
    except Exception as e:
        logf.write((f"FAILED TO START: {e}\n").encode())
        logf.close()
        return False, str(e)

    save_meta(name, {"name": name, "pid": proc.pid, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "cmd": cmd, "log": log_path, "link": link_path})
    return True, "Сессия запущена. Ссылка может появиться через 5–30 секунд."


def stop_session(name: str) -> None:
    meta = load_meta(name)
    pid = meta.get("pid")
    if pid:
        try:
            os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
            time.sleep(0.5)
        except Exception:
            pass
        try:
            if pid_alive(int(pid)):
                os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
        except Exception:
            pass
    # remove files
    try:
        for fn in ["meta.json", "link.txt"]:
            p = os.path.join(session_dir(name), fn)
            if os.path.exists(p):
                os.remove(p)
        os.rmdir(session_dir(name))
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "WLBPanel/2.0"

    def log_message(self, fmt, *args):
        return

    def send_html(self, title: str, body: str, code: int = 200):
        data = html_page(title, body)
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def require_auth(self) -> bool:
        if not is_authenticated(self):
            redirect(self, "/login")
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/login":
            self.login_page()
            return
        if path == "/logout":
            raw = self.headers.get("Cookie", "")
            try:
                c = cookies.SimpleCookie(raw)
                token = c.get("wlb_session")
                if token:
                    SESSION_TOKENS.discard(token.value)
            except Exception:
                pass
            self.send_response(302)
            self.send_header("Set-Cookie", "wlb_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if not self.require_auth():
            return
        if path == "/":
            self.index_page()
        elif path == "/settings":
            self.settings_page()
        elif path.startswith("/logs/"):
            name = path.split("/", 2)[2]
            self.logs_page(name)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            self.login_post()
            return
        if not self.require_auth():
            return
        if path == "/create":
            data = read_post(self)
            ok, msg = start_session(data.get("name", "").strip())
            redirect(self, "/?msg=" + ("ok" if ok else "err"))
        elif path.startswith("/delete/"):
            name = path.split("/", 2)[2]
            if NAME_RE.match(name):
                stop_session(name)
            redirect(self, "/")
        elif path == "/settings/password":
            data = read_post(self)
            p1, p2 = data.get("password1", ""), data.get("password2", "")
            if len(p1) < 8 or p1 != p2:
                redirect(self, "/settings?err=password")
                return
            set_password(p1)
            # invalidate all sessions except force relogin
            SESSION_TOKENS.clear()
            redirect(self, "/login?changed=1")
        elif path == "/settings/cookies":
            data = read_post(self)
            text = data.get("cookies", "").strip()
            if not text:
                try:
                    if os.path.exists(COOKIES_PATH):
                        os.remove(COOKIES_PATH)
                except Exception:
                    pass
                redirect(self, "/settings?cookies=deleted")
                return
            # Accept any text, but if it looks like JSON, validate it.
            if text[0] in "[{":
                try:
                    json.loads(text)
                except Exception:
                    redirect(self, "/settings?err=cookies_json")
                    return
            with open(COOKIES_PATH, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            try:
                os.chmod(COOKIES_PATH, 0o600)
            except Exception:
                pass
            redirect(self, "/settings?cookies=saved")
        else:
            self.send_error(404)

    def login_page(self):
        changed = "Пароль изменён. Войди заново." if "changed=1" in self.path else ""
        body = f"""
<div class="card" style="max-width:420px;margin:60px auto">
<h2>Вход</h2>
{('<div class="notice ok">'+html.escape(changed)+'</div>') if changed else ''}
<form method="post" action="/login">
<p><input name="username" placeholder="Логин" style="width:100%" autofocus></p>
<p><input name="password" placeholder="Пароль" type="password" style="width:100%"></p>
<p><button type="submit">Войти</button></p>
</form>
</div>"""
        self.send_html("Login", body)

    def login_post(self):
        data = read_post(self)
        cfg = load_config()
        if data.get("username") == cfg.get("username", "admin") and verify_password(data.get("password", ""), cfg):
            token = secrets.token_urlsafe(32)
            SESSION_TOKENS.add(token)
            self.send_response(302)
            self.send_header("Set-Cookie", f"wlb_session={token}; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self.send_html("Login", "<div class='card err'>Неверный логин или пароль. <a href='/login'>Назад</a></div>", 403)

    def index_page(self):
        warn = "" if cookies_ready() else "<div class='notice warn'><b>Cookies не загружены.</b> У тебя creator уже показал ошибку <code>--cookies is required</code>. Открой <a href='/settings'>Настройки</a> и вставь WB Stream cookies.</div>"
        rows = ""
        for name in list_sessions():
            meta = load_meta(name)
            pid = int(meta.get("pid", 0) or 0)
            alive = pid_alive(pid) if pid else False
            link = read_link(name)
            status = "active" if alive else "stopped"
            status_color = "green" if alive else "#b00020"
            link_html = f"<div class='copybox'><input readonly value='{html.escape(link)}' onclick='this.select()'><small>скопируй</small></div>" if link else "<code>ссылка ещё создаётся, обнови страницу через 5–15 секунд</code>"
            rows += f"""
<tr>
<td><b>{html.escape(name)}</b><br><small>PID {pid or '-'} · {html.escape(meta.get('created_at',''))}</small></td>
<td style="color:{status_color}">{status}</td>
<td>{link_html}</td>
<td><a href="/logs/{html.escape(name)}">Логи</a> &nbsp; <form style="display:inline" method="post" action="/delete/{html.escape(name)}" onsubmit="return confirm('Удалить ссылку {html.escape(name)}?')"><button class="danger">Удалить</button></form></td>
</tr>"""
        if not rows:
            rows = "<tr><td colspan='4'><span class='muted'>Пока нет активных ссылок.</span></td></tr>"
        body = f"""
{warn}
<div class="card">
<h2>Создать ссылку</h2>
<form method="post" action="/create">
<input name="name" placeholder="Например phone1 или factory" required>
<button type="submit">Создать ссылку</button>
</form>
<p class="muted">Имя: латиница/цифры/точка/дефис/подчёркивание. На каждое устройство лучше создавать отдельную ссылку.</p>
</div>
<div class="card">
<h2>Активные ссылки</h2>
<table><thead><tr><th>Имя</th><th>Статус</th><th>Join link</th><th>Действия</th></tr></thead><tbody>{rows}</tbody></table>
</div>
<div class="card">
<h2>Android</h2>
<p>Скопируй join link в приложение <b>whitelist-bypass.apk</b>, нажми Connect/GO и разреши VPN.</p>
<p class="muted">Creator запускается на сервере; телефон выступает Joiner.</p>
</div>"""
        self.send_html("WLB Panel", body)

    def settings_page(self):
        msg = ""
        qs = urlparse(self.path).query
        if "cookies=saved" in qs:
            msg = "<div class='notice ok'>Cookies сохранены.</div>"
        elif "cookies=deleted" in qs:
            msg = "<div class='notice ok'>Cookies удалены.</div>"
        elif "err=cookies_json" in qs:
            msg = "<div class='notice err'>Cookies выглядят как JSON, но JSON невалидный.</div>"
        elif "err=password" in qs:
            msg = "<div class='notice err'>Пароли не совпадают или короче 8 символов.</div>"
        current = ""
        try:
            if os.path.exists(COOKIES_PATH):
                with open(COOKIES_PATH, "r", encoding="utf-8") as f:
                    current = f.read()
        except Exception:
            current = ""
        ready = "загружены" if cookies_ready() else "не загружены"
        body = f"""
{msg}
<div class="card">
<h2>WB Stream cookies</h2>
<p>Статус: <b>{ready}</b>. Файл: <code>{html.escape(COOKIES_PATH)}</code></p>
<p class="muted">Если creator пишет <code>--cookies is required</code>, сюда нужно вставить cookies, экспортированные из whitelist-bypass desktop/браузера для WB Stream.</p>
<form method="post" action="/settings/cookies">
<textarea name="cookies" placeholder="Вставь сюда содержимое cookies-файла">{html.escape(current)}</textarea>
<p><button type="submit">Сохранить cookies</button> <button class="secondary" type="submit" onclick="document.querySelector('textarea[name=cookies]').value=''">Удалить cookies</button></p>
</form>
</div>
<div class="card">
<h2>Сменить пароль панели</h2>
<form method="post" action="/settings/password">
<p><input type="password" name="password1" placeholder="Новый пароль, минимум 8 символов" required style="width:320px"></p>
<p><input type="password" name="password2" placeholder="Повтори пароль" required style="width:320px"></p>
<p><button type="submit">Изменить пароль</button></p>
</form>
<p class="muted">После смены пароля тебя перекинет на страницу входа.</p>
</div>"""
        self.send_html("Settings", body)

    def logs_page(self, name: str):
        if not NAME_RE.match(name):
            self.send_error(400)
            return
        log = read_log(name)
        body = f"""
<div class="card">
<h2>Логи: {html.escape(name)}</h2>
<p><a href="/">← назад</a></p>
<pre style="white-space:pre-wrap;background:#111;color:#eee;padding:16px;border-radius:8px;overflow:auto">{html.escape(log)}</pre>
</div>"""
        self.send_html("Logs", body)


def main():
    cfg = load_config()
    print(f"WLB Panel listening on 0.0.0.0:{PORT}")
    print(f"Config: {CONFIG_PATH}")
    print(f"Creator: {CREATOR_BIN}")
    print(f"Cookies: {COOKIES_PATH} ready={cookies_ready()}")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
