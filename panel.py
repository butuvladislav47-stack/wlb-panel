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
import sys
import threading
import time
import urllib.parse
import urllib.request
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "wlb2 server 0.2.0"
APP_NAME = "WLB2 Panel"

HOST = os.environ.get("WLB_HOST", "0.0.0.0")
PORT = int(os.environ.get("WLB_PORT", "8088"))

BASE_DIR = Path(os.environ.get("WLB_BASE", "/var/lib/wlb2-panel"))
SESSIONS_DIR = BASE_DIR / "sessions"
CLIENTS_PATH = BASE_DIR / "clients.json"
LOG_DIR = Path(os.environ.get("WLB_LOGS", "/var/log/wlb2-panel"))
CONFIG_DIR = Path(os.environ.get("WLB_CONFIG_DIR", "/etc/wlb2-panel"))
CONFIG_PATH = Path(os.environ.get("WLB_CONFIG", str(CONFIG_DIR / "config.json")))
COOKIES_PATH = Path(os.environ.get("WLB_COOKIES", str(CONFIG_DIR / "wb-cookies.json")))
PANEL_DIR = Path(os.environ.get("WLB_PANEL_DIR", "/opt/wlb2-panel"))
CREATOR_BIN = Path(os.environ.get("WLB_CREATOR", "/opt/whitelist-bypass/headless-wbstream-creator"))

CHROME_PROFILE = BASE_DIR / "chrome-profile"
BROWSER_LOG = LOG_DIR / "browser.log"
BROWSER_STATE = BASE_DIR / "browser.json"
DISPLAY_NUM = os.environ.get("WLB_DISPLAY", ":99")
VNC_PORT = int(os.environ.get("WLB_VNC_PORT", "5901"))
NOVNC_PORT = int(os.environ.get("WLB_NOVNC_PORT", "6080"))
CDP_PORT = int(os.environ.get("WLB_CDP_PORT", "9222"))
VNC_PASSWORD_FILE = Path(os.environ.get("WLB_VNC_PASSWORD_FILE", str(CONFIG_DIR / "vnc.pass")))
VNC_PASSWORD_TEXT_PATH = CONFIG_DIR / "vnc-password.txt"

WB_LOGIN_URL = "https://stream.wb.ru/login"
WB_DEVICE_STORAGE_KEY = "wb_auth_api_device_id"
REQUIRED_COOKIE_NAMES = ["wbx-refresh", "x_wbaas_token", "_wbauid", "__wb_device_id", "wbx-validation-key"]

CLIENT_IDLE_SECONDS = int(os.environ.get("WLB_CLIENT_IDLE_SECONDS", "0"))
SESSION_LINK_TIMEOUT = int(os.environ.get("WLB_SESSION_LINK_TIMEOUT", "45"))
FALLBACK_COUNT = int(os.environ.get("WLB_FALLBACK_COUNT", "3"))

NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
state_lock = threading.RLock()


def ensure_dirs():
    for p in [BASE_DIR, SESSIONS_DIR, LOG_DIR, CONFIG_DIR, CHROME_PROFILE]:
        p.mkdir(parents=True, exist_ok=True)


ensure_dirs()


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def now_ts():
    return int(time.time())


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


def save_json(path: Path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def safe_name(name: str, fallback="item") -> str:
    name = NAME_RE.sub("-", (name or "").strip())
    name = name.strip(".-_")
    if not name:
        name = fallback
    return name[:60]


def token_short(token: str) -> str:
    return token[:8] + "..." + token[-6:] if len(token) > 16 else token


def get_config():
    return load_json(CONFIG_PATH, {})


def save_config(cfg):
    save_json(CONFIG_PATH, cfg)


def public_base_url(handler=None):
    cfg = get_config()
    base = str(cfg.get("public_base_url") or "").strip().rstrip("/")
    if base:
        return base
    if handler is not None:
        host = handler.headers.get("Host", "")
        if host:
            return "http://" + host
    return f"http://127.0.0.1:{PORT}"


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def creator_pid_alive(pid):
    if not pid_alive(pid):
        return False
    try:
        cmdline = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        return str(CREATOR_BIN) in cmdline
    except Exception:
        return False


def kill_pid(pid):
    if not pid:
        return
    try:
        pid = int(pid)
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            os.kill(pid, signal.SIGTERM)
        time.sleep(0.4)
        if pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def tail_file(path: Path, max_bytes=24000):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except Exception as e:
        return f"Cannot read log: {e}"


def cookie_status():
    if not COOKIES_PATH.exists():
        return {"ok": False, "message": "WB cookies are not saved", "names": []}
    try:
        data = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
        names = [x.get("name") for x in data if isinstance(x, dict)]
        missing = [x for x in REQUIRED_COOKIE_NAMES if x not in names]
        if "__wb_device_id" not in names:
            return {"ok": False, "message": "Missing __wb_device_id", "names": names}
        if missing:
            return {"ok": True, "message": "Cookies usable, optional cookies missing: " + ", ".join(missing), "names": names}
        return {"ok": True, "message": "Cookies OK", "names": names}
    except Exception as e:
        return {"ok": False, "message": f"Cookies JSON is broken: {e}", "names": []}


def load_clients():
    data = load_json(CLIENTS_PATH, {"clients": []})
    if not isinstance(data, dict) or not isinstance(data.get("clients"), list):
        return {"clients": []}
    return data


def save_clients(data):
    save_json(CLIENTS_PATH, data)


def find_client_by_token(token):
    if not token:
        return None
    for c in load_clients().get("clients", []):
        if hmac.compare_digest(str(c.get("token", "")), str(token)):
            return c
    return None


def find_client_by_id(client_id):
    for c in load_clients().get("clients", []):
        if c.get("id") == client_id:
            return c
    return None


def create_client(name, max_active=0):
    with state_lock:
        data = load_clients()
        client_id = safe_name(name, "client") + "-" + secrets.token_hex(3)
        token = secrets.token_urlsafe(32)
        client = {
            "id": client_id,
            "name": name.strip() or client_id,
            "token": token,
            "enabled": True,
            "max_active": int(max_active or 0),
            "fallback_sessions": [],
            "fallback_updated_at": "",
            "created_at": now(),
        }
        data["clients"].append(client)
        save_clients(data)
        return client


def delete_client(client_id, stop_sessions=True):
    with state_lock:
        data = load_clients()
        data["clients"] = [c for c in data.get("clients", []) if c.get("id") != client_id]
        save_clients(data)
        if stop_sessions:
            for s in list_sessions():
                if s.get("client_id") == client_id and s.get("running"):
                    stop_session(s["id"])


def toggle_client(client_id):
    with state_lock:
        data = load_clients()
        for c in data.get("clients", []):
            if c.get("id") == client_id:
                c["enabled"] = not bool(c.get("enabled", True))
                save_clients(data)
                return c
    return None


def update_client_limit(client_id, max_active):
    with state_lock:
        data = load_clients()
        for c in data.get("clients", []):
            if c.get("id") == client_id:
                c["max_active"] = int(max_active or 0)
                save_clients(data)
                return c
    return None


def session_dir(session_id):
    return SESSIONS_DIR / safe_name(session_id, "session")


def session_state(session_id):
    return load_json(session_dir(session_id) / "state.json", {})


def save_session_state(session_id, st):
    save_json(session_dir(session_id) / "state.json", st)


def list_sessions():
    out = []
    if not SESSIONS_DIR.exists():
        return out
    for d in sorted([x for x in SESSIONS_DIR.iterdir() if x.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True):
        st = load_json(d / "state.json", {})
        pid = st.get("pid")
        link = read_text(d / "link.txt", "").strip()
        running = bool(pid and creator_pid_alive(pid))
        out.append({
            "id": d.name,
            "pid": pid,
            "running": running,
            "link": link,
            "created_at": st.get("created_at", ""),
            "last_seen": st.get("last_seen", 0),
            "client_id": st.get("client_id", ""),
            "client_name": st.get("client_name", ""),
            "role": st.get("role", "active"),
            "log": str(d / "log.txt"),
            "managed": bool(st.get("managed")),
        })
    return out


def client_active_count(client_id):
    return sum(1 for s in list_sessions() if s.get("client_id") == client_id and s.get("running") and s.get("role") != "reserve")


def creator_command(link_path):
    return [
        str(CREATOR_BIN),
        "--resources", "moderate",
        "--cookies", str(COOKIES_PATH),
        "--write-file", str(link_path),
    ]


def wait_for_link(session_id, timeout=SESSION_LINK_TIMEOUT):
    d = session_dir(session_id)
    link_path = d / "link.txt"
    log_path = d / "log.txt"
    deadline = time.time() + timeout
    while time.time() < deadline:
        link = read_text(link_path, "").strip()
        if link.startswith("wbstream://"):
            return link
        st = session_state(session_id)
        pid = st.get("pid")
        if pid and not creator_pid_alive(pid):
            raise RuntimeError("Creator stopped before link was created:\n" + tail_file(log_path, 4000))
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for wbstream link after {timeout}s")


def session_link(session_id):
    link = read_text(session_dir(session_id) / "link.txt", "").strip()
    return link if link.startswith("wbstream://") else ""


def current_fallback_links(client):
    links = []
    for session_id in client.get("fallback_sessions", []) or []:
        st = session_state(session_id)
        if st.get("client_id") != client.get("id"):
            continue
        if not creator_pid_alive(st.get("pid")):
            continue
        link = session_link(session_id)
        if link:
            links.append(link)
    return links


def ensure_fallback_links(client, count=FALLBACK_COUNT, force=False):
    with state_lock:
        data = load_clients()
        live_client = None
        for c in data.get("clients", []):
            if c.get("id") == client.get("id"):
                live_client = c
                break
        if live_client is None:
            raise RuntimeError("Client not found")

        old_slots = list(live_client.get("fallback_sessions", []) or [])
        new_slots = []
        links = []
        for index in range(max(0, int(count))):
            old_id = old_slots[index] if index < len(old_slots) else ""
            link = ""
            reusable = False
            if old_id and not force:
                st = session_state(old_id)
                link = session_link(old_id)
                reusable = bool(st.get("client_id") == live_client.get("id") and creator_pid_alive(st.get("pid")) and link)
            if not reusable:
                if old_id:
                    delete_session(old_id)
                old_id, link = start_session(
                    f"{live_client.get('id', 'client')}-reserve-{index + 1}",
                    client=live_client,
                    managed=True,
                    role="reserve",
                )
            new_slots.append(old_id)
            links.append(link)

        for extra_id in old_slots[len(new_slots):]:
            delete_session(extra_id)

        live_client["fallback_sessions"] = new_slots
        live_client["fallback_updated_at"] = now()
        save_clients(data)
        return links, live_client


def start_session(name, client=None, managed=False, role="active"):
    cs = cookie_status()
    if not cs["ok"]:
        raise RuntimeError("Cannot create WB Stream link: " + cs["message"])
    if not CREATOR_BIN.exists():
        raise RuntimeError(f"Creator binary not found: {CREATOR_BIN}")

    session_id = safe_name(name, "session") + "-" + secrets.token_hex(4)
    d = session_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    log_path = d / "log.txt"
    link_path = d / "link.txt"
    try:
        link_path.unlink()
    except Exception:
        pass

    cmd = creator_command(link_path)
    with open(log_path, "a", encoding="utf-8", errors="replace") as log:
        log.write(f"\n=== START {now()} ===\nCMD: {' '.join(cmd)}\n")
        log.flush()
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    st = {
        "id": session_id,
        "pid": p.pid,
        "created_at": now(),
        "created_ts": now_ts(),
        "last_seen": now_ts(),
        "cmd": cmd,
        "managed": bool(managed),
        "role": role,
    }
    if client:
        st["client_id"] = client.get("id", "")
        st["client_name"] = client.get("name", "")
    save_session_state(session_id, st)

    if p.poll() is not None:
        raise RuntimeError("Creator stopped immediately:\n" + tail_file(log_path, 4000))
    link = wait_for_link(session_id)
    return session_id, link


def heartbeat_session(session_id):
    with state_lock:
        st = session_state(session_id)
        if not st:
            return False
        st["last_seen"] = now_ts()
        save_session_state(session_id, st)
        return True


def stop_session(session_id):
    with state_lock:
        st = session_state(session_id)
        if st.get("pid"):
            kill_pid(st.get("pid"))
        if st:
            st["stopped_at"] = now()
            save_session_state(session_id, st)


def delete_session(session_id):
    stop_session(session_id)
    shutil.rmtree(session_dir(session_id), ignore_errors=True)


def prune_stopped_sessions():
    if not SESSIONS_DIR.exists():
        return
    for d in [x for x in SESSIONS_DIR.iterdir() if x.is_dir()]:
        st = load_json(d / "state.json", {})
        pid = st.get("pid")
        link = read_text(d / "link.txt", "").strip()
        if pid and not creator_pid_alive(pid) and link.startswith("wbstream://"):
            shutil.rmtree(d, ignore_errors=True)


def cleanup_sessions():
    prune_stopped_sessions()
    if CLIENT_IDLE_SECONDS <= 0:
        return
    cutoff = now_ts() - CLIENT_IDLE_SECONDS
    for s in list_sessions():
        if s.get("running") and s.get("managed") and int(s.get("last_seen") or 0) < cutoff:
            stop_session(s["id"])


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
        return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)


def stop_browser_stack():
    st = load_json(BROWSER_STATE, {})
    for key in ["websockify", "x11vnc", "chrome", "openbox", "xvfb"]:
        pid = st.get(key)
        if pid:
            kill_pid(pid)
    save_json(BROWSER_STATE, {})


def start_browser_stack():
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
        return False, "Missing dependencies: " + ", ".join(missing)

    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY_NUM
    env["XDG_RUNTIME_DIR"] = str(BASE_DIR / "runtime")
    Path(env["XDG_RUNTIME_DIR"]).mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(env["XDG_RUNTIME_DIR"], 0o700)
    except Exception:
        pass

    launched = []
    try:
        p_xvfb = run_bg([xvfb, DISPLAY_NUM, "-screen", "0", "1280x800x24", "-ac"], BROWSER_LOG, env)
        launched.append(p_xvfb.pid)
        time.sleep(1.0)
        p_openbox = run_bg([openbox], BROWSER_LOG, env)
        launched.append(p_openbox.pid)
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
        launched.append(p_chrome.pid)
        time.sleep(2.0)
        if not VNC_PASSWORD_FILE.exists():
            raise RuntimeError(f"VNC password file not found: {VNC_PASSWORD_FILE}")
        p_x11vnc = run_bg([x11vnc, "-display", DISPLAY_NUM, "-forever", "-shared", "-rfbauth", str(VNC_PASSWORD_FILE), "-listen", "127.0.0.1", "-rfbport", str(VNC_PORT), "-noxdamage", "-repeat"], BROWSER_LOG, env)
        launched.append(p_x11vnc.pid)
        time.sleep(1.0)
        p_websockify = run_bg([websockify, "--web", "/usr/share/novnc", f"0.0.0.0:{NOVNC_PORT}", f"localhost:{VNC_PORT}"], BROWSER_LOG, env)
        launched.append(p_websockify.pid)
        time.sleep(1.0)
        dead = [pid for pid in launched if not pid_alive(pid)]
        if dead:
            raise RuntimeError("Browser stack process exited early: " + ", ".join(map(str, dead)))
        save_json(BROWSER_STATE, {
            "xvfb": p_xvfb.pid,
            "openbox": p_openbox.pid,
            "chrome": p_chrome.pid,
            "x11vnc": p_x11vnc.pid,
            "websockify": p_websockify.pid,
            "started_at": now(),
        })
        append_browser_log("Browser stack started OK")
        return True, "Server browser started"
    except Exception as e:
        append_browser_log("ERROR: " + repr(e))
        for pid in reversed(launched):
            kill_pid(pid)
        save_json(BROWSER_STATE, {})
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
    last = None
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=2) as r:
                arr = json.loads(r.read().decode("utf-8"))
            pages = [item for item in arr if item.get("type") == "page" and item.get("webSocketDebuggerUrl")]
            for item in pages:
                if "stream.wb.ru" in (item.get("url") or ""):
                    return item["webSocketDebuggerUrl"]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except Exception as e:
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"Chrome DevTools page is not ready: {last}")


def cdp_get_browser_ws_url():
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=3) as r:
        data = json.loads(r.read().decode("utf-8"))
    ws = data.get("webSocketDebuggerUrl")
    if not ws:
        raise RuntimeError("Chrome DevTools did not return browser WebSocket URL")
    return ws


def cdp_call(ws_url, method, params=None, timeout=8):
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
    page_ws = cdp_get_page_ws_url()
    browser_ws = cdp_get_browser_ws_url()
    try:
        result = cdp_call(browser_ws, "Storage.getCookies", {}, timeout=10)
    except Exception:
        try:
            cdp_call(page_ws, "Network.enable", {}, timeout=5)
        except Exception:
            pass
        result = cdp_call(page_ws, "Network.getAllCookies", {}, timeout=10)
    cookies_list = result.get("cookies", [])

    device_id = ""
    try:
        eval_result = cdp_call(page_ws, "Runtime.evaluate", {
            "expression": f"localStorage.getItem({json.dumps(WB_DEVICE_STORAGE_KEY)})",
            "returnByValue": True,
        }, timeout=5)
        device_id = str(eval_result.get("result", {}).get("value") or "").strip()
    except Exception as e:
        append_browser_log("Could not read WB device id from localStorage: " + repr(e))

    if device_id and not any(c.get("name") == "__wb_device_id" for c in cookies_list):
        cookies_list.append({"name": "__wb_device_id", "value": device_id, "domain": "stream.wb.ru"})
        append_browser_log("Imported __wb_device_id from WB localStorage")

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
    filtered.sort(key=lambda x: REQUIRED_COOKIE_NAMES.index(x["name"]) if x["name"] in REQUIRED_COOKIE_NAMES else 99)
    append_browser_log("Imported WB cookie names: " + ", ".join(x["name"] for x in filtered))
    if not filtered:
        raise RuntimeError("No wb.ru cookies found in server Chrome")
    save_json(COOKIES_PATH, filtered)
    st = cookie_status()
    if "__wb_device_id" not in st.get("names", []):
        raise RuntimeError("Cookies imported, but __wb_device_id was not found. Open stream.wb.ru in the server browser and try again.")
    return st


def check_admin(handler):
    cfg = get_config()
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        user, password = raw.split(":", 1)
    except Exception:
        return False
    if not hmac.compare_digest(user, cfg.get("username", "admin")):
        return False
    return hmac.compare_digest(sha256_hex(password), cfg.get("password_sha256", ""))


def esc(s):
    return html.escape(str(s), quote=True)


STYLE = """
body{font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;margin:0;background:#0b1020;color:#e9eefc}
a{color:#65d5ff;text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1200px;margin:0 auto;padding:26px}.top{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:18px}
h1{font-size:24px;margin:0}.muted{color:#9fb0d0}.card{background:#111a2e;border:1px solid #2a3657;border-radius:16px;padding:16px;margin:14px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
input,textarea,button{font:inherit;border-radius:10px;border:1px solid #2a3657;padding:10px;background:#0f1729;color:#e9eefc}
textarea{width:100%;min-height:170px;font-family:ui-monospace,Consolas,monospace}.btn,button{cursor:pointer;background:#7c5cff;border-color:#7c5cff;color:white;font-weight:700}
.danger{background:#b4233a;border-color:#b4233a}.ghost{background:#17213a;border-color:#2a3657}
table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #2a3657;padding:10px;text-align:left;vertical-align:top}
.pill{display:inline-block;border-radius:999px;padding:4px 10px;font-size:13px;border:1px solid #2a3657}.good{color:#39d98a}.bad{color:#ff6b6b}.warn{color:#ffcc66}
.link{font-family:ui-monospace,Consolas,monospace;word-break:break-all;background:#0f1729;border-radius:10px;padding:8px}
.pre{white-space:pre-wrap;background:#050812;border-radius:12px;padding:12px;max-height:520px;overflow:auto;font-family:ui-monospace,Consolas,monospace}
.small{font-size:13px}
"""


def render_page(title, body, flash=""):
    flash_html = f"<div class='card'><b>{esc(flash)}</b></div>" if flash else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{STYLE}</style></head><body><div class="wrap">
<div class="top"><div><h1>{APP_NAME}</h1><div class="muted">{VERSION}</div></div>
<div class="row"><a class="pill" href="/">Dashboard</a><a class="pill" href="/browser">WB Login Browser</a><a class="pill" href="/settings">Settings</a><a class="pill" href="/health">Health</a></div></div>
{flash_html}{body}</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "WLB2Panel"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def require_admin(self):
        if check_admin(self):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="wlb2-panel"')
        self.end_headers()
        self.wfile.write(b"Admin auth required")
        return False

    def send_html(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=True, indent=2).encode("utf-8"))

    def redirect(self, path="/"):
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def parse_post(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        data = self.rfile.read(length).decode("utf-8", errors="replace")
        return urllib.parse.parse_qs(data)

    def query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def path_only(self):
        return urllib.parse.urlparse(self.path).path

    def do_GET(self):
        try:
            p = self.path_only()
            if p == "/api/v1/client/start":
                return self.api_client_start()
            if p == "/api/v1/client/refresh":
                return self.api_client_refresh()
            if p == "/api/v1/client/heartbeat":
                return self.api_client_heartbeat()
            if p == "/api/v1/client/stop":
                return self.api_client_stop()
            if p.startswith("/c/"):
                return self.client_page(p.split("/c/", 1)[1].split("/", 1)[0])

            if not self.require_admin():
                return
            if p == "/":
                return self.index()
            if p == "/browser":
                return self.browser()
            if p == "/settings":
                return self.settings()
            if p == "/health":
                return self.health()
            if p == "/browser-log":
                return self.browser_log()
            if p.startswith("/logs/"):
                return self.logs(p.split("/logs/", 1)[1])
            self.send_html(render_page("Not found", "<div class='card'>Not found</div>"), 404)
        except Exception as e:
            self.send_html(render_page("Error", f"<div class='card'><h2>Error</h2><div class='pre'>{esc(repr(e))}</div></div>"), 500)

    def do_POST(self):
        try:
            p = self.path_only()
            if p == "/api/v1/client/start":
                return self.api_client_start()
            if p == "/api/v1/client/refresh":
                return self.api_client_refresh()
            if p == "/api/v1/client/heartbeat":
                return self.api_client_heartbeat()
            if p == "/api/v1/client/stop":
                return self.api_client_stop()

            if not self.require_admin():
                return
            if p == "/create-client":
                return self.create_client_post()
            if p == "/delete-client":
                return self.delete_client_post()
            if p == "/toggle-client":
                return self.toggle_client_post()
            if p == "/update-client-limit":
                return self.update_client_limit_post()
            if p == "/refresh-client-fallback":
                return self.refresh_client_fallback_post()
            if p == "/start-manual":
                return self.start_manual_post()
            if p == "/stop-session":
                return self.stop_session_post()
            if p == "/delete-session":
                return self.delete_session_post()
            if p == "/start-browser":
                ok, msg = start_browser_stack()
                return self.browser(msg)
            if p == "/stop-browser":
                stop_browser_stack()
                return self.browser("Server browser stopped")
            if p == "/import-cookies":
                st = import_cookies_from_chrome()
                return self.browser("Cookies imported: " + st["message"])
            if p == "/save-cookies":
                return self.save_cookies_post()
            if p == "/change-password":
                return self.change_password_post()
            if p == "/save-public-url":
                return self.save_public_url_post()
            self.redirect("/")
        except Exception as e:
            self.send_html(render_page("Error", f"<div class='card'><h2>Error</h2><div class='pre'>{esc(repr(e))}</div></div><p><a href='/'>Back</a></p>"), 500)

    def api_token(self):
        q = self.query()
        token = (q.get("token", [""])[0] or "").strip()
        if token:
            return token
        if self.command == "POST":
            form = self.parse_post()
            return (form.get("token", [""])[0] or "").strip()
        return ""

    def api_client_refresh(self):
        cleanup_sessions()
        token = self.api_token()
        client = find_client_by_token(token)
        if not client:
            return self.send_json({"ok": False, "error": "invalid token"}, 403)
        if not client.get("enabled", True):
            return self.send_json({"ok": False, "error": "client disabled"}, 403)
        q = self.query()
        force = (q.get("force", ["0"])[0] or "0") in ("1", "true", "yes")
        links, live_client = ensure_fallback_links(client, FALLBACK_COUNT, force=force)
        return self.send_json({
            "ok": True,
            "version": VERSION,
            "client_id": live_client.get("id"),
            "client_name": live_client.get("name"),
            "fallback_links": links,
            "fallback_count": len(links),
            "fallback_updated_at": live_client.get("fallback_updated_at", ""),
        })

    def api_client_start(self):
        cleanup_sessions()
        token = self.api_token()
        client = find_client_by_token(token)
        if not client:
            return self.send_json({"ok": False, "error": "invalid token"}, 403)
        if not client.get("enabled", True):
            return self.send_json({"ok": False, "error": "client disabled"}, 403)
        max_active = int(client.get("max_active") or 0)
        if max_active > 0 and client_active_count(client["id"]) >= max_active:
            return self.send_json({"ok": False, "error": "active session limit reached"}, 429)
        session_name = f"{client.get('id', 'client')}-{int(time.time())}"
        with state_lock:
            session_id, link = start_session(session_name, client=client, managed=True, role="active")
        base = public_base_url(self)
        latest_client = find_client_by_token(token) or client
        return self.send_json({
            "ok": True,
            "version": VERSION,
            "client_id": client.get("id"),
            "client_name": client.get("name"),
            "session_id": session_id,
            "join_link": link,
            "heartbeat_url": f"{base}/api/v1/client/heartbeat?token={urllib.parse.quote(token)}&session_id={urllib.parse.quote(session_id)}",
            "stop_url": f"{base}/api/v1/client/stop?token={urllib.parse.quote(token)}&session_id={urllib.parse.quote(session_id)}",
            "heartbeat_interval_seconds": 30,
            "fallback_links": current_fallback_links(latest_client),
        })

    def api_client_heartbeat(self):
        token = self.api_token()
        client = find_client_by_token(token)
        if not client or not client.get("enabled", True):
            return self.send_json({"ok": False, "error": "invalid token"}, 403)
        q = self.query()
        session_id = (q.get("session_id", [""])[0] or "").strip()
        st = session_state(session_id)
        if not st or st.get("client_id") != client.get("id"):
            return self.send_json({"ok": False, "error": "session not found"}, 404)
        heartbeat_session(session_id)
        return self.send_json({"ok": True, "running": creator_pid_alive(st.get("pid"))})

    def api_client_stop(self):
        token = self.api_token()
        client = find_client_by_token(token)
        if not client:
            return self.send_json({"ok": False, "error": "invalid token"}, 403)
        q = self.query()
        session_id = (q.get("session_id", [""])[0] or "").strip()
        st = session_state(session_id)
        if not st or st.get("client_id") != client.get("id"):
            return self.send_json({"ok": False, "error": "session not found"}, 404)
        stop_session(session_id)
        return self.send_json({"ok": True})

    def client_page(self, token):
        client = find_client_by_token(token)
        if not client or not client.get("enabled", True):
            return self.send_html(render_page("Client link", "<div class='card'><h2>Client link disabled</h2></div>"), 404)
        base = public_base_url(self)
        start_url = f"{base}/api/v1/client/start?token={urllib.parse.quote(token)}"
        refresh_url = f"{base}/api/v1/client/refresh?token={urllib.parse.quote(token)}"
        body = f"""
<div class="card"><h2>WLB2 client link</h2>
<p>This permanent link is for the mobile WLB2 client app. The app refreshes 3 reserve <code>wbstream://</code> links when the server is reachable and requests a fresh active link on Connect.</p>
<p><b>Client:</b> {esc(client.get('name'))}</p>
<div class="link">{esc(start_url)}</div>
<p class="muted small">Reserve refresh API: {esc(refresh_url)}</p>
<p><a class="pill" href="{esc(start_url)}">Start now and show JSON</a></p>
<p><a class="pill" href="{esc(refresh_url)}">Refresh reserve and show JSON</a></p>
</div>"""
        self.send_html(render_page("Client link", body))

    def index(self, flash=""):
        cleanup_sessions()
        cs = cookie_status()
        base = public_base_url(self)
        client_rows = []
        for c in load_clients().get("clients", []):
            client_link = f"{base}/c/{c.get('token')}"
            start_api = f"{base}/api/v1/client/start?token={c.get('token')}"
            active = client_active_count(c.get("id"))
            fallback_ready = len(current_fallback_links(c))
            status = "<span class='good'>enabled</span>" if c.get("enabled", True) else "<span class='bad'>disabled</span>"
            client_rows.append(f"""
<tr><td><b>{esc(c.get('name'))}</b><br><span class="muted small">{esc(c.get('id'))}</span></td>
<td>{status}<br><span class="muted small">active: {active}, reserve: {fallback_ready}/{FALLBACK_COUNT}, limit: {esc(c.get('max_active') or 'unlimited')}</span></td>
<td><div class="link">{esc(client_link)}</div><div class="muted small">API: {esc(start_api)}</div></td>
<td><form method="post" action="/toggle-client"><input type="hidden" name="id" value="{esc(c.get('id'))}"><button class="ghost">Toggle</button></form>
<form method="post" action="/update-client-limit" class="row"><input type="hidden" name="id" value="{esc(c.get('id'))}"><input name="max_active" value="{esc(c.get('max_active') or 0)}" style="width:86px"><button class="ghost">Limit</button></form>
<form method="post" action="/refresh-client-fallback"><input type="hidden" name="id" value="{esc(c.get('id'))}"><button class="ghost">Refresh reserve</button></form>
<form method="post" action="/delete-client" onsubmit="return confirm('Delete client?')"><input type="hidden" name="id" value="{esc(c.get('id'))}"><button class="danger">Delete</button></form></td></tr>""")
        if not client_rows:
            client_rows.append("<tr><td colspan='4' class='muted'>No clients yet.</td></tr>")

        session_rows = []
        for s in list_sessions():
            status = "<span class='good'>running</span>" if s.get("running") else "<span class='bad'>stopped</span>"
            label = s.get("client_name") or "manual"
            session_rows.append(f"""
<tr><td><b>{esc(label)}</b><br><span class="muted small">{esc(s.get('id'))}<br>{esc(s.get('created_at'))}</span></td>
<td>{status}<br><span class="muted small">PID {esc(s.get('pid') or '')}</span></td>
<td><div class="link">{esc(s.get('link') or 'waiting for link')}</div></td>
<td><a href="/logs/{esc(s.get('id'))}">Logs</a>
<form method="post" action="/stop-session"><input type="hidden" name="id" value="{esc(s.get('id'))}"><button class="ghost">Stop</button></form>
<form method="post" action="/delete-session" onsubmit="return confirm('Delete session?')"><input type="hidden" name="id" value="{esc(s.get('id'))}"><button class="danger">Delete</button></form></td></tr>""")
        if not session_rows:
            session_rows.append("<tr><td colspan='4' class='muted'>No sessions yet.</td></tr>")

        body = f"""
<div class="grid">
<div class="card"><h2>Status</h2><p><span class="pill {'good' if cs['ok'] else 'bad'}">{esc(cs['message'])}</span></p>
<p class="muted">Permanent client links survive reboot. Temporary WB Stream rooms are created on demand.</p></div>
<div class="card"><h2>Create client</h2><form method="post" action="/create-client" class="row">
<input name="name" placeholder="Family phone / Mom / Dad" required><input name="max_active" value="0" title="0 = unlimited" style="width:90px"><button>Create</button></form>
<p class="muted small">Each API start call creates a separate WB Stream room. The same client link can be used by several devices.</p></div>
<div class="card"><h2>Manual test</h2><form method="post" action="/start-manual" class="row"><input name="name" placeholder="test-phone"><button>Create wbstream</button></form></div>
</div>
<div class="card"><h2>Clients</h2><table><thead><tr><th>Client</th><th>Status</th><th>Permanent link</th><th>Actions</th></tr></thead><tbody>{''.join(client_rows)}</tbody></table></div>
<div class="card"><h2>Active and recent sessions</h2><table><thead><tr><th>Owner</th><th>Status</th><th>Temporary wbstream</th><th>Actions</th></tr></thead><tbody>{''.join(session_rows)}</tbody></table></div>
"""
        self.send_html(render_page("Dashboard", body, flash))

    def create_client_post(self):
        form = self.parse_post()
        create_client(form.get("name", [""])[0], form.get("max_active", ["0"])[0])
        self.redirect("/")

    def delete_client_post(self):
        form = self.parse_post()
        delete_client(form.get("id", [""])[0], stop_sessions=True)
        self.redirect("/")

    def toggle_client_post(self):
        form = self.parse_post()
        toggle_client(form.get("id", [""])[0])
        self.redirect("/")

    def update_client_limit_post(self):
        form = self.parse_post()
        update_client_limit(form.get("id", [""])[0], form.get("max_active", ["0"])[0])
        self.redirect("/")

    def refresh_client_fallback_post(self):
        form = self.parse_post()
        client = find_client_by_id(form.get("id", [""])[0])
        if not client:
            raise RuntimeError("Client not found")
        links, _ = ensure_fallback_links(client, FALLBACK_COUNT, force=True)
        self.index(f"Reserve refreshed: {len(links)} links ready")

    def start_manual_post(self):
        form = self.parse_post()
        name = safe_name(form.get("name", ["manual"])[0], "manual")
        session_id, link = start_session(name, client=None, managed=False)
        self.index(f"Manual session created: {session_id} / {link}")

    def stop_session_post(self):
        form = self.parse_post()
        stop_session(form.get("id", [""])[0])
        self.redirect("/")

    def delete_session_post(self):
        form = self.parse_post()
        delete_session(form.get("id", [""])[0])
        self.redirect("/")

    def browser(self, flash=""):
        running = browser_running()
        host = self.headers.get("Host", "").split(":")[0] or "127.0.0.1"
        novnc_url = f"http://{host}:{NOVNC_PORT}/vnc.html?autoconnect=true&resize=scale"
        cs = cookie_status()
        vnc_password = read_text(VNC_PASSWORD_TEXT_PATH, "").strip()
        body = f"""
<div class="card"><h2>WB Login Browser</h2>
<p>Start the server browser, open noVNC, log in to WB Stream once, then import cookies. The Chrome profile and imported cookies survive server reboot.</p>
<p>noVNC password: <b>{esc(vnc_password or 'not found')}</b></p>
<div class="row"><form method="post" action="/start-browser"><button>Start browser</button></form>
<form method="post" action="/import-cookies"><button class="ghost">Import cookies</button></form>
<form method="post" action="/stop-browser"><button class="danger">Stop browser</button></form>
<a class="pill" target="_blank" href="{esc(novnc_url)}">Open noVNC</a></div>
<p>Browser: <span class="pill {'good' if running else 'warn'}">{'running' if running else 'stopped'}</span> Cookies: <span class="pill {'good' if cs['ok'] else 'bad'}">{esc(cs['message'])}</span></p></div>
<div class="card"><h2>Browser log</h2><div class="pre">{esc(tail_file(BROWSER_LOG, 8000))}</div></div>
"""
        self.send_html(render_page("WB Login Browser", body, flash))

    def settings(self, flash=""):
        cfg = get_config()
        cookies_text = read_text(COOKIES_PATH, "")
        body = f"""
<div class="grid">
<div class="card"><h2>Public server URL</h2><p class="muted">Optional. Example: http://132.243.194.148:8088</p>
<form method="post" action="/save-public-url"><input name="public_base_url" style="width:100%" value="{esc(cfg.get('public_base_url',''))}"><p><button>Save URL</button></p></form></div>
<div class="card"><h2>Change admin password</h2><form method="post" action="/change-password">
<p><input type="password" name="old" placeholder="Current password"></p><p><input type="password" name="new1" placeholder="New password"></p><p><input type="password" name="new2" placeholder="Repeat new password"></p><button>Change password</button></form></div>
</div>
<div class="card"><h2>WB cookies</h2><form method="post" action="/save-cookies"><textarea name="cookies_json" spellcheck="false">{esc(cookies_text)}</textarea><p><button>Save cookies manually</button></p></form></div>
"""
        self.send_html(render_page("Settings", body, flash))

    def save_cookies_post(self):
        form = self.parse_post()
        txt = form.get("cookies_json", [""])[0].strip()
        if not txt:
            try:
                COOKIES_PATH.unlink()
            except Exception:
                pass
            return self.settings("Cookies cleared")
        data = json.loads(txt)
        if not isinstance(data, list):
            raise RuntimeError("Cookies must be a JSON array")
        clean = []
        for x in data:
            if isinstance(x, dict) and x.get("name") and x.get("value"):
                clean.append({"name": x["name"], "value": x["value"]})
        save_json(COOKIES_PATH, clean)
        self.settings("Cookies saved")

    def change_password_post(self):
        form = self.parse_post()
        old = form.get("old", [""])[0]
        n1 = form.get("new1", [""])[0]
        n2 = form.get("new2", [""])[0]
        cfg = get_config()
        if not hmac.compare_digest(sha256_hex(old), cfg.get("password_sha256", "")):
            raise RuntimeError("Current password is wrong")
        if n1 != n2:
            raise RuntimeError("New passwords do not match")
        if len(n1) < 8:
            raise RuntimeError("Password must be at least 8 characters")
        cfg["password_sha256"] = sha256_hex(n1)
        save_config(cfg)
        self.settings("Password changed")

    def save_public_url_post(self):
        form = self.parse_post()
        cfg = get_config()
        cfg["public_base_url"] = form.get("public_base_url", [""])[0].strip().rstrip("/")
        save_config(cfg)
        self.settings("Public URL saved")

    def logs(self, session_id):
        p = session_dir(session_id) / "log.txt"
        body = f"<div class='card'><h2>Logs: {esc(session_id)}</h2><div class='pre'>{esc(tail_file(p))}</div><p><a class='pill' href='/'>Back</a></p></div>"
        self.send_html(render_page("Logs", body))

    def browser_log(self):
        body = f"<div class='card'><h2>Browser log</h2><div class='pre'>{esc(tail_file(BROWSER_LOG))}</div><p><a class='pill' href='/browser'>Back</a></p></div>"
        self.send_html(render_page("Browser log", body))

    def health(self):
        checks = []
        for label, names in [("Xvfb", ["Xvfb"]), ("openbox", ["openbox"]), ("x11vnc", ["x11vnc"]), ("websockify", ["websockify"]), ("Chrome", ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"])]:
            val = find_executable(names)
            checks.append((label, bool(val), val or "not found"))
        checks.append(("creator", CREATOR_BIN.exists() and os.access(CREATOR_BIN, os.X_OK), str(CREATOR_BIN)))
        rows = "".join([f"<tr><td>{esc(a)}</td><td><span class='pill {'good' if b else 'bad'}'>{'OK' if b else 'FAIL'}</span></td><td>{esc(c)}</td></tr>" for a, b, c in checks])
        body = f"<div class='card'><h2>Health</h2><table>{rows}</table><p>Cookies: {esc(cookie_status()['message'])}</p><p>Browser running: {browser_running()}</p><p>Client idle cleanup: {CLIENT_IDLE_SECONDS or 'disabled'}</p></div>"
        self.send_html(render_page("Health", body))


def main():
    cfg = get_config()
    if not cfg.get("username") or not cfg.get("password_sha256"):
        pw = secrets.token_urlsafe(12)
        cfg = {"username": "admin", "password_sha256": sha256_hex(pw)}
        save_config(cfg)
        print("Generated admin password:", pw)
    cleanup_sessions()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"{APP_NAME} {VERSION} listening on {HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
