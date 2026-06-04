#!/usr/bin/env python3
import base64
import hashlib
import hmac
import html
import http.cookies
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_VERSION = "5.0"
HOST = "0.0.0.0"
PORT = int(os.environ.get("WLB_PANEL_PORT", "8088"))
NOVNC_PORT = int(os.environ.get("WLB_NOVNC_PORT", "6080"))

CONFIG_PATH = Path("/etc/wlb-panel/config.json")
DATA_DIR = Path("/var/lib/wlb-panel")
SESSIONS_DIR = DATA_DIR / "sessions"
LOG_DIR = Path("/var/log/wlb-panel")
COOKIE_PATH = Path("/etc/wlb-panel/wb-cookies.json")
BROWSER_STATE = DATA_DIR / "browser.json"
BROWSER_PROFILE = DATA_DIR / "browser-profile"
CREATOR_BIN = Path("/opt/whitelist-bypass/headless-wbstream-creator")

WB_LOGIN_URL = "https://stream.wb.ru/login"

for p in [DATA_DIR, SESSIONS_DIR, LOG_DIR, CONFIG_PATH.parent, BROWSER_PROFILE]:
    p.mkdir(parents=True, exist_ok=True)


def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def load_json(path, default):
    try:
        if Path(path).exists():
            return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()


def read_config():
    return load_json(CONFIG_PATH, {"username": "admin", "password_sha256": sha256("admin"), "session_token": ""})


def write_config(c):
    save_json(CONFIG_PATH, c)


def safe_name(name):
    name = (name or "").strip()
    name = re.sub(r"[^a-zA-Z0-9а-яА-Я._-]+", "-", name)
    name = name.strip(".-_")
    return name[:48]


def is_pid_running(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def kill_pid(pid):
    if not pid:
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
        time.sleep(0.5)
        if is_pid_running(pid):
            os.kill(int(pid), signal.SIGKILL)
    except Exception:
        pass


def shell_quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def cookie_status():
    if not COOKIE_PATH.exists():
        return {"ok": False, "message": "Cookies не сохранены"}
    try:
        data = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
        names = []
        if isinstance(data, list):
            names = [x.get("name") for x in data if isinstance(x, dict)]
        elif isinstance(data, dict):
            # Allow {cookies:[...]} too.
            names = [x.get("name") for x in data.get("cookies", []) if isinstance(x, dict)]
        has_device = "__wb_device_id" in names
        return {"ok": bool(has_device), "message": "OK: __wb_device_id найден" if has_device else "Нет __wb_device_id. Нужен экспорт из Creator/серверного браузера."}
    except Exception as e:
        return {"ok": False, "message": f"Cookies JSON повреждён: {e}"}


def list_sessions():
    items = []
    for d in sorted(SESSIONS_DIR.iterdir()) if SESSIONS_DIR.exists() else []:
        if not d.is_dir():
            continue
        meta = load_json(d / "meta.json", {})
        name = meta.get("name", d.name)
        pid = meta.get("pid")
        link = ""
        if (d / "link.txt").exists():
            link = (d / "link.txt").read_text(encoding="utf-8", errors="ignore").strip()
        log_path = LOG_DIR / f"{d.name}.log"
        status = "running" if is_pid_running(pid) else "stopped"
        if status == "running" and not link:
            status = "starting"
        items.append({"id": d.name, "name": name, "pid": pid, "link": link, "status": status, "log": str(log_path)})
    return items


def start_session(name, resources="moderate"):
    sid = safe_name(name)
    if not sid:
        raise ValueError("Имя ссылки пустое")
    sdir = SESSIONS_DIR / sid
    if sdir.exists():
        raise ValueError("Ссылка с таким именем уже существует")
    if not CREATOR_BIN.exists():
        raise RuntimeError(f"Не найден {CREATOR_BIN}")
    sdir.mkdir(parents=True, exist_ok=False)
    link_path = sdir / "link.txt"
    log_path = LOG_DIR / f"{sid}.log"
    cmd = [str(CREATOR_BIN), "--write-file", str(link_path), "--resources", resources]
    if COOKIE_PATH.exists():
        cmd += ["--cookies", str(COOKIE_PATH)]
    with open(log_path, "ab", buffering=0) as log:
        log.write(f"\n=== START {now()} ===\n".encode())
        log.write(("CMD: " + " ".join(shell_quote(x) for x in cmd) + "\n").encode())
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    save_json(sdir / "meta.json", {"name": name, "id": sid, "pid": p.pid, "created_at": now(), "resources": resources})
    return sid


def delete_session(sid):
    sid = safe_name(sid)
    sdir = SESSIONS_DIR / sid
    meta = load_json(sdir / "meta.json", {})
    kill_pid(meta.get("pid"))
    if sdir.exists():
        shutil.rmtree(sdir, ignore_errors=True)


def last_log(sid, lines=200):
    sid = safe_name(sid)
    path = LOG_DIR / f"{sid}.log"
    if not path.exists():
        return "Лог пуст"
    data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:])


def which_any(names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def browser_procs():
    return load_json(BROWSER_STATE, {})


def stop_browser():
    st = browser_procs()
    for key in ["chrome", "websockify", "x11vnc", "openbox", "xvfb"]:
        kill_pid(st.get(key))
    save_json(BROWSER_STATE, {})


def start_browser():
    stop_browser()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    blog = open(LOG_DIR / "browser.log", "ab", buffering=0)
    blog.write(f"\n=== BROWSER START {now()} ===\n".encode())
    display = ":99"
    env = os.environ.copy()
    env["DISPLAY"] = display
    xvfb = subprocess.Popen(["Xvfb", display, "-screen", "0", "1366x768x24", "-ac"], stdout=blog, stderr=subprocess.STDOUT)
    time.sleep(1)
    openbox_bin = which_any(["openbox", "fluxbox"])
    openbox = None
    if openbox_bin:
        openbox = subprocess.Popen([openbox_bin], env=env, stdout=blog, stderr=subprocess.STDOUT)
    time.sleep(1)
    x11vnc = subprocess.Popen(["x11vnc", "-display", display, "-nopw", "-forever", "-shared", "-rfbport", "5900"], stdout=blog, stderr=subprocess.STDOUT)
    time.sleep(1)
    novnc_web = "/usr/share/novnc"
    if not Path(novnc_web).exists():
        novnc_web = "/usr/share/novnc/utils/novnc_proxy"
    websockify_cmd = ["websockify", "--web", "/usr/share/novnc", f"0.0.0.0:{NOVNC_PORT}", "127.0.0.1:5900"]
    websockify = subprocess.Popen(websockify_cmd, stdout=blog, stderr=subprocess.STDOUT)
    chrome_bin = which_any(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"])
    if not chrome_bin:
        blog.write(b"ERROR: chrome/chromium not found\n")
        raise RuntimeError("Chrome/Chromium не найден")
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    chrome_cmd = [
        chrome_bin,
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1366,768",
        "--start-maximized",
        f"--user-data-dir={BROWSER_PROFILE}",
        f"--app={WB_LOGIN_URL}",
    ]
    chrome = subprocess.Popen(chrome_cmd, env=env, stdout=blog, stderr=subprocess.STDOUT)
    save_json(BROWSER_STATE, {"xvfb": xvfb.pid, "openbox": openbox.pid if openbox else None, "x11vnc": x11vnc.pid, "websockify": websockify.pid, "chrome": chrome.pid, "started_at": now()})


def browser_running():
    st = browser_procs()
    return is_pid_running(st.get("websockify")) and is_pid_running(st.get("x11vnc"))


# Chrome cookies extraction is intentionally best-effort. Modern Chrome may encrypt cookies;
# if extraction fails, user can paste cookies manually from Creator/export.
def import_chrome_cookies():
    # We do not implement decryption here. Instead, check if browser created profile and show user-friendly message.
    # Keeping this endpoint avoids crashes and provides guidance.
    sqlite_path = BROWSER_PROFILE / "Default" / "Network" / "Cookies"
    if not sqlite_path.exists():
        sqlite_path = BROWSER_PROFILE / "Default" / "Cookies"
    if not sqlite_path.exists():
        raise RuntimeError("Файл cookies Chrome ещё не найден. Залогинься в WB Stream и подожди 5 секунд.")
    raise RuntimeError("Автоимпорт cookies из Chrome может быть зашифрован. Используй Export Cookies из Creator или вставь JSON вручную в Настройках.")


class Handler(BaseHTTPRequestHandler):
    server_version = f"WLBPanel/{APP_VERSION}"

    def log_message(self, fmt, *args):
        super().log_message(fmt, *args)

    def send_html(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def redirect(self, path):
        self.send_response(302)
        self.send_header("Location", path)
        self.end_headers()

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length).decode("utf-8", errors="replace")

    def form(self):
        return {k: v[0] if v else "" for k, v in parse_qs(self.read_body()).items()}

    def cookies(self):
        c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return {k: v.value for k, v in c.items()}

    def is_auth(self):
        cfg = read_config()
        return bool(cfg.get("session_token")) and hmac.compare_digest(self.cookies().get("wlb_session", ""), cfg.get("session_token", ""))

    def require_auth(self):
        if not self.is_auth():
            self.redirect("/login")
            return False
        return True

    def page(self, title, content):
        user = html.escape(read_config().get("username", "admin"))
        return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · WLB Panel v{APP_VERSION}</title>
<style>
:root{{--bg:#0f172a;--panel:#111827;--card:#172033;--text:#e5e7eb;--muted:#94a3b8;--accent:#7c3aed;--accent2:#06b6d4;--danger:#ef4444;--ok:#22c55e;--warn:#f59e0b;--border:#293548}}
*{{box-sizing:border-box}} body{{margin:0;background:radial-gradient(circle at top left,#1e1b4b,#0f172a 42%,#020617);color:var(--text);font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}}
a{{color:#93c5fd;text-decoration:none}} .wrap{{display:flex;min-height:100vh}} .side{{width:260px;background:rgba(15,23,42,.82);border-right:1px solid var(--border);padding:22px;position:fixed;top:0;bottom:0}}
.logo{{font-size:22px;font-weight:800;letter-spacing:.2px;margin-bottom:8px}} .ver{{color:var(--muted);font-size:12px;margin-bottom:26px}} .nav a{{display:block;padding:12px 14px;border-radius:12px;margin:8px 0;color:var(--text)}} .nav a:hover{{background:#1f2937}} .main{{margin-left:260px;width:calc(100% - 260px);padding:28px;}}
.top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}} .h1{{font-size:26px;font-weight:800}} .user{{color:var(--muted);font-size:14px}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}}
.card{{background:rgba(17,24,39,.78);border:1px solid var(--border);box-shadow:0 16px 50px rgba(0,0,0,.25);border-radius:18px;padding:18px;margin-bottom:16px}}
.btn{{border:0;border-radius:12px;padding:10px 14px;background:linear-gradient(135deg,var(--accent),var(--accent2));color:white;font-weight:700;cursor:pointer}} .btn.secondary{{background:#334155}} .btn.danger{{background:var(--danger)}} .btn.ok{{background:var(--ok)}}
input,textarea,select{{width:100%;background:#0b1220;border:1px solid var(--border);border-radius:12px;color:var(--text);padding:11px 12px;outline:none}} textarea{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;min-height:180px}}
label{{display:block;font-size:13px;color:var(--muted);margin:10px 0 6px}} table{{width:100%;border-collapse:collapse}} th,td{{padding:12px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}} th{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}}
.badge{{display:inline-block;padding:4px 9px;border-radius:999px;font-size:12px;font-weight:700}} .running{{background:#052e16;color:#86efac}} .starting{{background:#3b2f08;color:#fde68a}} .stopped{{background:#3f1d1d;color:#fecaca}}
.copy{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;word-break:break-all;color:#bfdbfe}} .alert{{padding:12px 14px;border:1px solid var(--border);background:#0b1220;border-radius:14px;margin-bottom:16px}} .alert.warn{{border-color:#92400e;color:#fde68a}} .alert.ok{{border-color:#166534;color:#86efac}} .alert.err{{border-color:#991b1b;color:#fecaca}}
pre{{white-space:pre-wrap;background:#020617;border:1px solid var(--border);border-radius:14px;padding:14px;overflow:auto;color:#cbd5e1}} iframe{{width:100%;height:720px;border:1px solid var(--border);border-radius:16px;background:#000}}
@media(max-width:800px){{.side{{position:static;width:100%;height:auto}}.wrap{{display:block}}.main{{margin-left:0;width:100%;padding:16px}}}}
</style></head><body><div class="wrap"><aside class="side"><div class="logo">WLB Panel</div><div class="ver">v{APP_VERSION} · WB Stream</div><nav class="nav"><a href="/">Ссылки</a><a href="/browser">WB Login Browser</a><a href="/settings">Настройки</a><a href="/logout">Выйти</a></nav></aside><main class="main"><div class="top"><div class="h1">{html.escape(title)}</div><div class="user">{user}</div></div>{content}</main></div></body></html>"""

    def do_HEAD(self):
        self.send_response(200); self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/login": return self.login_get()
        if p == "/logout": return self.logout()
        if not self.require_auth(): return
        if p == "/": return self.index()
        if p == "/settings": return self.settings()
        if p == "/browser": return self.browser()
        if p.startswith("/logs/"): return self.logs(p.split("/",2)[2])
        self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/login": return self.login_post()
        if not self.require_auth(): return
        try:
            if p == "/session/create": return self.session_create()
            if p == "/session/delete": return self.session_delete()
            if p == "/settings/password": return self.change_password()
            if p == "/settings/cookies": return self.save_cookies()
            if p == "/browser/start": return self.browser_start()
            if p == "/browser/stop": return self.browser_stop()
            if p == "/browser/import": return self.browser_import()
        except Exception as e:
            return self.send_html(self.page("Ошибка", f"<div class='alert err'><b>Ошибка:</b> {html.escape(str(e))}</div><p><a href='javascript:history.back()'>Назад</a></p>"), 500)
        self.send_error(404)

    def login_get(self):
        body = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Login · WLB Panel</title><style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at top,#1e1b4b,#020617);font-family:Inter,system-ui;color:#e5e7eb}}.box{{width:360px;background:rgba(15,23,42,.9);border:1px solid #293548;border-radius:22px;padding:26px;box-shadow:0 25px 70px rgba(0,0,0,.45)}}h1{{margin:0 0 8px}}p{{color:#94a3b8}}input{{width:100%;padding:12px;border-radius:12px;border:1px solid #293548;background:#020617;color:#e5e7eb;margin:8px 0 12px}}button{{width:100%;padding:12px;border:0;border-radius:12px;background:linear-gradient(135deg,#7c3aed,#06b6d4);color:white;font-weight:800}}</style></head><body><form class="box" method="post" action="/login"><h1>WLB Panel</h1><p>Вход в панель v{APP_VERSION}</p><input name="username" placeholder="Логин" autofocus><input name="password" type="password" placeholder="Пароль"><button>Войти</button></form></body></html>"""
        self.send_html(body)

    def login_post(self):
        f = self.form(); c = read_config()
        if f.get("username") == c.get("username") and hmac.compare_digest(sha256(f.get("password","")), c.get("password_sha256","")):
            token = secrets.token_urlsafe(32); c["session_token"] = token; write_config(c)
            self.send_response(302); self.send_header("Location", "/"); self.send_header("Set-Cookie", f"wlb_session={token}; Path=/; HttpOnly; SameSite=Lax"); self.end_headers(); return
        self.send_html("Неверный логин или пароль", 403)

    def logout(self):
        c = read_config(); c["session_token"] = ""; write_config(c)
        self.send_response(302); self.send_header("Location", "/login"); self.send_header("Set-Cookie", "wlb_session=; Path=/; Max-Age=0"); self.end_headers()

    def index(self):
        cs = cookie_status()
        alert = f"<div class='alert {'ok' if cs['ok'] else 'warn'}'>{html.escape(cs['message'])}</div>"
        rows = ""
        for s in list_sessions():
            badge = f"<span class='badge {s['status']}'>{s['status']}</span>"
            link = f"<div class='copy'>{html.escape(s['link'])}</div>" if s['link'] else "<span style='color:#94a3b8'>ссылка создаётся...</span>"
            rows += f"<tr><td><b>{html.escape(s['name'])}</b><br><small>{html.escape(s['id'])}</small></td><td>{badge}</td><td>{link}</td><td><a class='btn secondary' href='/logs/{html.escape(s['id'])}'>Логи</a> <form method='post' action='/session/delete' style='display:inline'><input type='hidden' name='id' value='{html.escape(s['id'])}'><button class='btn danger'>Удалить</button></form></td></tr>"
        if not rows:
            rows = "<tr><td colspan='4' style='color:#94a3b8'>Ссылок пока нет</td></tr>"
        content = f"""{alert}<div class='card'><h2>Создать WB Stream ссылку</h2><form method='post' action='/session/create'><label>Имя ссылки</label><input name='name' placeholder='phone1 / factory / test'><label>Режим ресурсов</label><select name='resources'><option value='moderate'>moderate — для слабого VPS</option><option value='default'>default</option><option value='unlimited'>unlimited</option></select><br><br><button class='btn'>Создать ссылку</button></form></div><div class='card'><h2>Ссылки</h2><table><tr><th>Имя</th><th>Статус</th><th>Join link</th><th>Действия</th></tr>{rows}</table></div>"""
        self.send_html(self.page("Ссылки", content))

    def session_create(self):
        f = self.form(); start_session(f.get("name"), f.get("resources") or "moderate"); self.redirect("/")

    def session_delete(self):
        delete_session(self.form().get("id")); self.redirect("/")

    def logs(self, sid):
        self.send_html(self.page("Логи", f"<div class='card'><h2>{html.escape(sid)}</h2><pre>{html.escape(last_log(sid))}</pre><p><a class='btn secondary' href='/'>Назад</a></p></div>"))

    def settings(self):
        cs = cookie_status()
        cookies_text = COOKIE_PATH.read_text(encoding="utf-8", errors="ignore") if COOKIE_PATH.exists() else ""
        content = f"""<div class='grid'><div class='card'><h2>Сменить пароль</h2><form method='post' action='/settings/password'><label>Новый пароль</label><input name='p1' type='password'><label>Повтор</label><input name='p2' type='password'><br><br><button class='btn'>Сменить пароль</button></form></div><div class='card'><h2>WB Stream cookies</h2><div class='alert {'ok' if cs['ok'] else 'warn'}'>{html.escape(cs['message'])}</div><form method='post' action='/settings/cookies'><label>Cookies JSON</label><textarea name='cookies' placeholder='Вставь JSON cookies'>{html.escape(cookies_text)}</textarea><br><br><button class='btn'>Сохранить cookies</button></form></div></div>"""
        self.send_html(self.page("Настройки", content))

    def change_password(self):
        f = self.form(); p1 = f.get("p1", ""); p2 = f.get("p2", "")
        if p1 != p2: raise RuntimeError("Пароли не совпадают")
        if len(p1) < 8: raise RuntimeError("Пароль должен быть минимум 8 символов")
        c = read_config(); c["password_sha256"] = sha256(p1); c["session_token"] = ""; write_config(c)
        self.redirect("/login")

    def save_cookies(self):
        raw = self.form().get("cookies", "").strip()
        if not raw: raise RuntimeError("Cookies пустые")
        data = json.loads(raw)
        COOKIE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.redirect("/settings")

    def browser(self):
        running = browser_running()
        st = browser_procs()
        novnc_url = f"http://{self.headers.get('Host','').split(':')[0]}:{NOVNC_PORT}/vnc.html?autoconnect=1&resize=remote&path=websockify"
        content = f"""<div class='card'><h2>WB Login Browser</h2><p style='color:#94a3b8'>Нажми запуск — снизу должен открыться серверный Chrome сразу на <b>{WB_LOGIN_URL}</b>. Залогинься в WB Stream. Если автоимпорт cookies не сработает, используй Export Cookies/ручную вставку в Настройках.</p><div class='alert {'ok' if running else 'warn'}'>Статус браузера: {'запущен' if running else 'не запущен'}</div><form method='post' action='/browser/start' style='display:inline'><button class='btn'>Запустить серверный браузер</button></form> <form method='post' action='/browser/stop' style='display:inline'><button class='btn danger'>Остановить</button></form> <form method='post' action='/browser/import' style='display:inline'><button class='btn secondary'>Импортировать cookies из браузера</button></form><p><a href='{html.escape(novnc_url)}' target='_blank'>Открыть noVNC в отдельной вкладке</a></p></div><div class='card'><iframe src='{html.escape(novnc_url)}'></iframe></div><div class='card'><h3>Browser log</h3><pre>{html.escape((LOG_DIR/'browser.log').read_text(encoding='utf-8', errors='ignore')[-6000:] if (LOG_DIR/'browser.log').exists() else 'лог пуст')}</pre></div>"""
        self.send_html(self.page("WB Login Browser", content))

    def browser_start(self):
        start_browser(); time.sleep(1); self.redirect("/browser")

    def browser_stop(self):
        stop_browser(); self.redirect("/browser")

    def browser_import(self):
        try:
            import_chrome_cookies()
        except Exception as e:
            self.send_html(self.page("Импорт cookies", f"<div class='alert warn'>{html.escape(str(e))}</div><p><a class='btn secondary' href='/browser'>Назад</a></p>")); return
        self.redirect("/settings")


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"WLB Panel v{APP_VERSION} listening on {HOST}:{PORT}", flush=True)
    httpd.serve_forever()

if __name__ == "__main__":
    main()
