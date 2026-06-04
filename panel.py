#!/usr/bin/env python3
import base64
import hashlib
import html
import http.cookies
import json
import os
import secrets
import shutil
import signal
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_VERSION = "v6"
CONFIG_PATH = Path("/etc/wlb-panel/config.json")
STATE_DIR = Path("/var/lib/wlb-panel")
SESS_DIR = STATE_DIR / "sessions"
LOG_DIR = Path("/var/log/wlb-panel")
BROWSER_LOG = LOG_DIR / "browser.log"
PORT_DEFAULT = 8088
NOVNC_DEFAULT = 6080

PROCS = {}
BROWSER_PROCS = []

def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}

def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)

def cfg():
    return load_json(CONFIG_PATH, {})

def esc(x):
    return html.escape(str(x or ""), quote=True)

def chrome_bin():
    for c in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]:
        p = shutil.which(c)
        if p:
            return p
    return ""

def kill_browser():
    global BROWSER_PROCS
    for p in BROWSER_PROCS:
        try:
            p.terminate()
        except Exception:
            pass
    time.sleep(0.5)
    for p in BROWSER_PROCS:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass
    BROWSER_PROCS = []

def start_browser():
    kill_browser()
    c = cfg()
    novnc_port = int(c.get("novnc_port", NOVNC_DEFAULT))
    profile = Path(c.get("chrome_profile", str(STATE_DIR / "chrome-profile")))
    profile.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(BROWSER_LOG, "a", buffering=1)
    log.write("\n=== START BROWSER %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    ch = chrome_bin()
    if not ch:
        log.write("ERROR: no Chrome/Chromium binary found\n")
        return False, "Chrome/Chromium не найден"
    display = ":99"
    env = os.environ.copy()
    env["DISPLAY"] = display
    cmds = [
        ["Xvfb", display, "-screen", "0", "1280x800x24", "-ac"],
        ["openbox"],
        [ch, "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--window-size=1280,800", f"--user-data-dir={profile}", "--app=https://stream.wb.ru/login"],
        ["x11vnc", "-display", display, "-forever", "-shared", "-nopw", "-listen", "0.0.0.0", "-rfbport", "5901"],
        ["websockify", "--web", "/usr/share/novnc", "0.0.0.0:%d" % novnc_port, "localhost:5901"],
    ]
    names = ["Xvfb", "openbox", "chrome", "x11vnc", "websockify"]
    try:
        for name, cmd in zip(names, cmds):
            log.write("CMD[%s]: %s\n" % (name, " ".join(cmd)))
            p = subprocess.Popen(cmd, stdout=log, stderr=log, env=env)
            BROWSER_PROCS.append(p)
            time.sleep(1.1 if name in ("Xvfb", "chrome") else 0.4)
        return True, "Серверный браузер запущен"
    except Exception as e:
        log.write("ERROR: %r\n" % e)
        return False, str(e)

def import_browser_cookies():
    c = cfg()
    profile = Path(c.get("chrome_profile", str(STATE_DIR / "chrome-profile")))
    # Chrome stores encrypted cookies; direct extraction is unreliable on Linux without libsecret.
    # Instead, keep manual export available and provide clear message.
    return False, "Автоимпорт cookies из Chromium в MVP недоступен. Используй Export Cookies из creator/app или вставь JSON вручную в Настройки."

def cookies_status():
    c = cfg()
    path = Path(c.get("cookies_path", "/etc/wlb-panel/wb-cookies.json"))
    if not path.exists():
        return "Нет cookies", False
    txt = path.read_text(encoding="utf-8", errors="ignore")
    ok = "__wb_device_id" in txt
    return ("OK: __wb_device_id найден" if ok else "Cookies есть, но __wb_device_id не найден"), ok

def start_session(name):
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_ .")[:60].strip()
    if not safe:
        return False, "Имя пустое"
    sdir = SESS_DIR / safe
    sdir.mkdir(parents=True, exist_ok=True)
    link = sdir / "link.txt"
    pidf = sdir / "pid.txt"
    logf = LOG_DIR / f"{safe}.log"
    if safe in PROCS and PROCS[safe].poll() is None:
        return False, "Сессия уже запущена"
    c = cfg()
    creator = c.get("creator_path", "/opt/whitelist-bypass/headless-wbstream-creator")
    cookies = c.get("cookies_path", "/etc/wlb-panel/wb-cookies.json")
    cmd = [creator, "--write-file", str(link), "--resources", "moderate"]
    if Path(cookies).exists():
        cmd += ["--cookies", cookies]
    f = open(logf, "a", buffering=1)
    f.write("\n=== START %s ===\nCMD: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(cmd)))
    p = subprocess.Popen(cmd, stdout=f, stderr=f)
    PROCS[safe] = p
    pidf.write_text(str(p.pid), encoding="utf-8")
    return True, f"Запущено, PID {p.pid}"

def stop_session(name):
    safe = name
    if safe in PROCS and PROCS[safe].poll() is None:
        try:
            PROCS[safe].terminate()
            time.sleep(0.5)
            if PROCS[safe].poll() is None:
                PROCS[safe].kill()
        except Exception:
            pass
    sdir = SESS_DIR / safe
    pidf = sdir / "pid.txt"
    if pidf.exists():
        try:
            pid = int(pidf.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    if sdir.exists():
        shutil.rmtree(sdir, ignore_errors=True)
    return True

def list_sessions():
    out = []
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    for d in sorted(SESS_DIR.iterdir()):
        if not d.is_dir(): continue
        name = d.name
        link = (d / "link.txt").read_text(encoding="utf-8", errors="ignore").strip() if (d / "link.txt").exists() else ""
        pid = (d / "pid.txt").read_text(encoding="utf-8", errors="ignore").strip() if (d / "pid.txt").exists() else ""
        running = False
        if pid:
            try:
                os.kill(int(pid), 0); running = True
            except Exception:
                running = False
        out.append({"name": name, "link": link, "pid": pid, "running": running})
    return out

CSS = """
:root{--bg:#0b1020;--card:#121a33;--card2:#162140;--text:#eaf0ff;--muted:#9fb0d0;--accent:#7c5cff;--accent2:#24d3ee;--danger:#ff5c7a;--ok:#3ee68b;--warn:#ffd166}*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,Arial,sans-serif;background:radial-gradient(circle at top left,#1e2a5a 0,#0b1020 40%,#070a14 100%);color:var(--text)}a{color:#9fdcff;text-decoration:none}.wrap{max-width:1120px;margin:0 auto;padding:28px}.nav{display:flex;gap:12px;align-items:center;justify-content:space-between;margin-bottom:22px}.brand{font-weight:800;font-size:22px;letter-spacing:.2px}.badge{font-size:12px;color:#06101e;background:linear-gradient(90deg,var(--accent2),#8ff0ff);padding:4px 8px;border-radius:999px;margin-left:8px}.tabs a{display:inline-block;padding:10px 14px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.08);border-radius:12px;margin-left:8px}.tabs a:hover,.btn:hover{filter:brightness(1.12)}.card{background:linear-gradient(180deg,rgba(255,255,255,.075),rgba(255,255,255,.035));border:1px solid rgba(255,255,255,.1);border-radius:20px;padding:20px;margin:16px 0;box-shadow:0 18px 50px rgba(0,0,0,.22)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.btn{border:0;border-radius:12px;padding:10px 14px;font-weight:700;color:white;background:linear-gradient(90deg,var(--accent),#4d8dff);cursor:pointer}.btn.danger{background:linear-gradient(90deg,var(--danger),#ff8a5c)}.btn.ghost{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.12)}input,textarea{width:100%;padding:12px 13px;border-radius:12px;border:1px solid rgba(255,255,255,.16);background:#0d1428;color:var(--text);outline:none}textarea{min-height:180px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}.muted{color:var(--muted)}.ok{color:var(--ok)}.warn{color:var(--warn)}.danger-text{color:var(--danger)}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid rgba(255,255,255,.08);padding:12px;text-align:left;vertical-align:top}.linkbox{font-family:ui-monospace,Menlo,Consolas,monospace;background:#081024;border:1px solid rgba(255,255,255,.1);padding:10px;border-radius:10px;word-break:break-all}.iframe{width:100%;height:720px;border:0;border-radius:16px;background:#000}.log{white-space:pre-wrap;background:#061024;border-radius:14px;padding:14px;font-size:12px;overflow:auto;max-height:520px}.login{max-width:420px;margin:12vh auto}.hero{font-size:34px;margin:6px 0 4px}.small{font-size:12px}@media(max-width:800px){.grid{grid-template-columns:1fr}.tabs{display:flex;flex-wrap:wrap}.tabs a{margin:4px}.wrap{padding:16px}}
"""

def page(title, body, authed=True):
    nav = ""
    if authed:
        nav = f"""<div class='nav'><div class='brand'>WLB Panel <span class='badge'>{APP_VERSION}</span></div><div class='tabs'><a href='/'>Ссылки</a><a href='/browser'>WB Login Browser</a><a href='/settings'>Настройки</a><a href='/logout'>Выход</a></div></div>"""
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{esc(title)}</title><style>{CSS}</style></head><body><div class='wrap'>{nav}{body}</div></body></html>""".encode()

class H(BaseHTTPRequestHandler):
    server_version = "WLBPanel/6"
    def send_html(self, b, code=200):
        self.send_response(code); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def redirect(self, p):
        self.send_response(302); self.send_header("Location",p); self.end_headers()
    def read_form(self):
        n = int(self.headers.get("Content-Length","0") or 0)
        return parse_qs(self.rfile.read(n).decode(errors="ignore"))
    def cookie_token(self):
        ck = http.cookies.SimpleCookie(self.headers.get("Cookie","")); return ck.get("session","").value if "session" in ck else ""
    def authed(self):
        return bool(self.cookie_token()) and self.cookie_token() == cfg().get("session_token")
    def require(self):
        if not self.authed(): self.redirect("/login"); return False
        return True
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/login": return self.login_get()
        if p == "/logout": return self.logout()
        if not self.require(): return
        if p == "/": return self.index()
        if p == "/settings": return self.settings()
        if p == "/browser": return self.browser()
        if p.startswith("/logs/"): return self.logs(p.split("/",2)[2])
        if p == "/browser-log": return self.browser_log()
        self.send_error(404)
    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/login": return self.login_post()
        if not self.require(): return
        if p == "/create": return self.create()
        if p == "/delete": return self.delete()
        if p == "/save-cookies": return self.save_cookies()
        if p == "/change-password": return self.change_password()
        if p == "/start-browser": return self.start_browser_post()
        if p == "/stop-browser": kill_browser(); return self.redirect("/browser")
        if p == "/import-browser-cookies": return self.import_cookies_post()
        self.send_error(404)
    def login_get(self):
        b = """<div class='login'><div class='card'><div class='brand'>WLB Panel <span class='badge'>v6</span></div><h1 class='hero'>Вход</h1><p class='muted'>Введите логин и пароль из установщика.</p><form method='post' action='/login'><p><input name='username' placeholder='login' autofocus></p><p><input name='password' type='password' placeholder='password'></p><button class='btn'>Войти</button></form></div></div>"""
        self.send_html(page("Login", b, False))
    def login_post(self):
        f = self.read_form(); c = cfg()
        u = f.get("username",[""])[0]; p = f.get("password",[""])[0]
        if u == c.get("username") and hashlib.sha256(p.encode()).hexdigest() == c.get("password_sha256"):
            token = secrets.token_urlsafe(32); c["session_token"] = token; save_json(CONFIG_PATH, c)
            self.send_response(302); self.send_header("Set-Cookie",f"session={token}; Path=/; HttpOnly; SameSite=Lax"); self.send_header("Location","/"); self.end_headers(); return
        self.send_html(page("Login", "<div class='login'><div class='card'><h2>Ошибка входа</h2><p class='danger-text'>Неверный логин или пароль.</p><a class='btn ghost' href='/login'>Назад</a></div></div>", False), 403)
    def logout(self):
        c=cfg(); c["session_token"]=""; save_json(CONFIG_PATH,c); self.redirect("/login")
    def index(self):
        status, ok = cookies_status()
        rows = ""
        for s in list_sessions():
            link = s["link"] or "ссылка ещё создаётся..."
            st = "active" if s["running"] else "stopped"
            cls = "ok" if s["running"] else "warn"
            rows += f"<tr><td><b>{esc(s['name'])}</b><br><span class='{cls}'>{st}</span><br><span class='small muted'>PID: {esc(s['pid'])}</span></td><td><div class='linkbox'>{esc(link)}</div></td><td><a class='btn ghost' href='/logs/{esc(s['name'])}'>Логи</a> <form method='post' action='/delete' style='display:inline'><input type='hidden' name='name' value='{esc(s['name'])}'><button class='btn danger'>Удалить</button></form></td></tr>"
        if not rows: rows = "<tr><td colspan='3' class='muted'>Ссылок пока нет.</td></tr>"
        body = f"""<div class='grid'><div class='card'><h2>Создать WB Stream ссылку</h2><p class='muted'>Одна ссылка — одно устройство. Для стабильности создавай отдельные ссылки.</p><form method='post' action='/create'><input name='name' placeholder='phone1 / factory / ya' required><p><button class='btn'>Создать ссылку</button></p></form></div><div class='card'><h2>Cookies</h2><p class='{('ok' if ok else 'warn')}'>{esc(status)}</p><p class='muted'>Если ссылки зависают, проверь логи и наличие <code>__wb_device_id</code>.</p><a class='btn ghost' href='/settings'>Открыть настройки</a></div></div><div class='card'><h2>Ссылки</h2><table><tr><th>Имя</th><th>Join link</th><th>Действия</th></tr>{rows}</table></div>"""
        self.send_html(page("Links", body))
    def settings(self):
        status, ok = cookies_status()
        current = Path(cfg().get("cookies_path","/etc/wlb-panel/wb-cookies.json"))
        txt = current.read_text(encoding="utf-8", errors="ignore") if current.exists() else ""
        body = f"""<div class='grid'><div class='card'><h2>Сменить пароль</h2><form method='post' action='/change-password'><p><input type='password' name='p1' placeholder='Новый пароль, минимум 8 символов'></p><p><input type='password' name='p2' placeholder='Повтори пароль'></p><button class='btn'>Сохранить пароль</button></form></div><div class='card'><h2>WB Stream cookies</h2><p class='{('ok' if ok else 'warn')}'>{esc(status)}</p><p class='muted'>Вставь JSON cookies. Нужен <code>__wb_device_id</code>.</p><form method='post' action='/save-cookies'><textarea name='cookies'>{esc(txt)}</textarea><p><button class='btn'>Сохранить cookies</button></p></form></div></div>"""
        self.send_html(page("Settings", body))
    def browser(self):
        c=cfg(); novnc=int(c.get("novnc_port",NOVNC_DEFAULT))
        url=f"http://{self.headers.get('Host','').split(':')[0]}:{novnc}/vnc.html?autoconnect=true&resize=remote"
        status, ok = cookies_status()
        body=f"""<div class='card'><h2>WB Login Browser</h2><p class='muted'>Нажми запуск — снизу должен открыться серверный Chrome сразу на <code>https://stream.wb.ru/login</code>.</p><form method='post' action='/start-browser' style='display:inline'><button class='btn'>Запустить серверный браузер</button></form> <form method='post' action='/stop-browser' style='display:inline'><button class='btn danger'>Остановить браузер</button></form> <form method='post' action='/import-browser-cookies' style='display:inline'><button class='btn ghost'>Импорт cookies из браузера</button></form><p class='{('ok' if ok else 'warn')}'>{esc(status)}</p><p><a class='btn ghost' href='/browser-log'>Логи браузера</a></p></div><div class='card'><iframe class='iframe' src='{esc(url)}'></iframe></div>"""
        self.send_html(page("Browser", body))
    def create(self):
        f=self.read_form(); ok,msg=start_session(f.get("name",[""])[0]); self.redirect("/")
    def delete(self):
        f=self.read_form(); stop_session(f.get("name",[""])[0]); self.redirect("/")
    def save_cookies(self):
        f=self.read_form(); txt=f.get("cookies",[""])[0].strip()
        try:
            data=json.loads(txt)
            if not isinstance(data, list): raise ValueError("Cookies JSON должен быть массивом")
        except Exception as e:
            self.send_html(page("Cookies error",f"<div class='card'><h2>Ошибка cookies</h2><p class='danger-text'>{esc(e)}</p><a class='btn ghost' href='/settings'>Назад</a></div>"),400); return
        path=Path(cfg().get("cookies_path","/etc/wlb-panel/wb-cookies.json")); path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8"); self.redirect("/settings")
    def change_password(self):
        f=self.read_form(); p1=f.get("p1",[""])[0]; p2=f.get("p2",[""])[0]
        if p1!=p2 or len(p1)<8:
            self.send_html(page("Password error","<div class='card'><h2>Ошибка</h2><p class='danger-text'>Пароли не совпадают или короче 8 символов.</p><a class='btn ghost' href='/settings'>Назад</a></div>"),400); return
        c=cfg(); c["password_sha256"]=hashlib.sha256(p1.encode()).hexdigest(); c["session_token"]=""; save_json(CONFIG_PATH,c); self.redirect("/login")
    def start_browser_post(self):
        ok,msg=start_browser(); self.redirect("/browser")
    def import_cookies_post(self):
        ok,msg=import_browser_cookies(); self.send_html(page("Import cookies",f"<div class='card'><h2>Импорт cookies</h2><p class='{('ok' if ok else 'warn')}'>{esc(msg)}</p><a class='btn ghost' href='/browser'>Назад</a></div>"))
    def logs(self,name):
        safe="".join(ch for ch in name if ch.isalnum() or ch in "-_ .")[:60].strip()
        p=LOG_DIR/f"{safe}.log"; txt=p.read_text(encoding="utf-8",errors="ignore") if p.exists() else "Лог пуст"
        self.send_html(page("Logs",f"<div class='card'><h2>Логи: {esc(safe)}</h2><pre class='log'>{esc(txt[-20000:])}</pre><a class='btn ghost' href='/'>Назад</a></div>"))
    def browser_log(self):
        txt=BROWSER_LOG.read_text(encoding="utf-8",errors="ignore") if BROWSER_LOG.exists() else "Лог пуст"
        self.send_html(page("Browser logs",f"<div class='card'><h2>Логи браузера</h2><pre class='log'>{esc(txt[-25000:])}</pre><a class='btn ghost' href='/browser'>Назад</a></div>"))

if __name__ == "__main__":
    c=cfg(); port=int(c.get("port",PORT_DEFAULT))
    SESS_DIR.mkdir(parents=True, exist_ok=True); LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"WLB Panel {APP_VERSION} starting on 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
