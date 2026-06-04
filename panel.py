#!/usr/bin/env python3
import base64
import hashlib
import hmac
import html
import json
import os
import re
import signal
import subprocess
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs

CONFIG_PATH = os.environ.get('WLB_CONFIG', '/etc/wlb-panel/config.json')
STATE_PATH = os.environ.get('WLB_STATE', '/var/lib/wlb-panel/state.json')
SESSIONS_DIR = os.environ.get('WLB_SESSIONS', '/var/lib/wlb-panel/sessions')
LOG_DIR = os.environ.get('WLB_LOGS', '/var/log/wlb-panel')
CREATOR_BIN = os.environ.get('WLB_CREATOR', '/opt/whitelist-bypass/headless-wbstream-creator')
HOST = os.environ.get('WLB_HOST', '0.0.0.0')
PORT = int(os.environ.get('WLB_PORT', '8088'))
DEFAULT_RESOURCES = os.environ.get('WLB_RESOURCES', 'moderate')

NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$')


def ensure_dirs():
    for p in [os.path.dirname(STATE_PATH), SESSIONS_DIR, LOG_DIR]:
        os.makedirs(p, exist_ok=True)


def load_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {'sessions': {}}
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'sessions': {}}


def save_state(state):
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def proc_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def kill_pid(pid):
    try:
        pid = int(pid)
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        if proc_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def read_text(path, max_bytes=8192):
    try:
        with open(path, 'rb') as f:
            data = f.read(max_bytes)
        return data.decode('utf-8', errors='replace').strip()
    except Exception:
        return ''


def tail_file(path, max_bytes=20000):
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = f.read()
        return data.decode('utf-8', errors='replace')
    except Exception as e:
        return f'Cannot read log: {e}'


def create_session(name):
    name = name.strip()
    if not NAME_RE.match(name):
        raise ValueError('Имя должно начинаться с буквы/цифры и содержать только латиницу, цифры, точку, дефис или подчёркивание. Максимум 63 символа.')
    state = load_state()
    if name in state['sessions'] and proc_alive(state['sessions'][name].get('pid')):
        raise ValueError('Сессия с таким именем уже запущена.')

    session_dir = os.path.join(SESSIONS_DIR, name)
    os.makedirs(session_dir, exist_ok=True)
    link_path = os.path.join(session_dir, 'link.txt')
    pid_path = os.path.join(session_dir, 'process.pid')
    log_path = os.path.join(LOG_DIR, f'{name}.log')

    open(link_path, 'w').close()
    logf = open(log_path, 'ab', buffering=0)
    cmd = [CREATOR_BIN, '--write-file', link_path, '--resources', DEFAULT_RESOURCES]
    # Start as a new process group so delete can kill the creator and children.
    p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=session_dir, start_new_session=True)
    with open(pid_path, 'w') as f:
        f.write(str(p.pid))

    state['sessions'][name] = {
        'name': name,
        'pid': p.pid,
        'created_at': int(time.time()),
        'link_path': link_path,
        'log_path': log_path,
        'resources': DEFAULT_RESOURCES,
    }
    save_state(state)
    return p.pid


def delete_session(name):
    state = load_state()
    sess = state.get('sessions', {}).get(name)
    if sess:
        kill_pid(sess.get('pid'))
        state['sessions'].pop(name, None)
        save_state(state)
    return True


def check_auth(handler):
    cfg = load_config()
    auth = handler.headers.get('Authorization', '')
    if not auth.startswith('Basic '):
        return False
    try:
        raw = base64.b64decode(auth.split(' ', 1)[1]).decode('utf-8')
        user, password = raw.split(':', 1)
    except Exception:
        return False
    if not hmac.compare_digest(user, cfg.get('username', 'admin')):
        return False
    expected = cfg.get('password_sha256', '')
    got = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(got, expected)


STYLE = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:24px;background:#f6f7f9;color:#111}
.card{background:#fff;border:1px solid #ddd;border-radius:12px;padding:18px;margin-bottom:18px;box-shadow:0 1px 3px #0001}
input,button{font:inherit;padding:10px;border-radius:8px;border:1px solid #bbb}button{cursor:pointer;background:#111;color:#fff;border-color:#111}button.danger{background:#b00020;border-color:#b00020}.muted{color:#666}.ok{color:#087b28}.bad{color:#b00020}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #eee;padding:10px;text-align:left;vertical-align:top}.link{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all;background:#f0f0f0;padding:8px;border-radius:6px}.row-actions form{display:inline}pre{white-space:pre-wrap;background:#111;color:#eee;padding:12px;border-radius:8px;max-height:420px;overflow:auto}.top{display:flex;justify-content:space-between;align-items:center;gap:12px}
"""


class Handler(BaseHTTPRequestHandler):
    server_version = 'wlb-panel/0.1'

    def require_auth(self):
        if check_auth(self):
            return True
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="wlb-panel"')
        self.end_headers()
        self.wfile.write(b'Auth required')
        return False

    def redirect(self, path='/'):
        self.send_response(303)
        self.send_header('Location', path)
        self.end_headers()

    def send_html(self, body, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        page = f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WLB Panel</title><style>{STYLE}</style></head><body>{body}</body></html>'
        self.wfile.write(page.encode('utf-8'))

    def do_GET(self):
        if not self.require_auth():
            return
        if self.path.startswith('/logs/'):
            name = self.path.split('/logs/', 1)[1].split('?',1)[0]
            state = load_state()
            sess = state.get('sessions', {}).get(name)
            if not sess:
                self.send_html('<div class="card">Сессия не найдена. <a href="/">Назад</a></div>', 404)
                return
            log = html.escape(tail_file(sess.get('log_path','')))
            self.send_html(f'<div class="card"><a href="/">← Назад</a><h2>Логи: {html.escape(name)}</h2><pre>{log}</pre></div>')
            return
        self.render_index()

    def do_POST(self):
        if not self.require_auth():
            return
        length = int(self.headers.get('Content-Length', '0') or '0')
        data = self.rfile.read(length).decode('utf-8', errors='replace')
        form = parse_qs(data)
        try:
            if self.path == '/create':
                name = form.get('name', [''])[0]
                create_session(name)
                self.redirect('/')
                return
            if self.path == '/delete':
                name = form.get('name', [''])[0]
                delete_session(name)
                self.redirect('/')
                return
        except Exception as e:
            self.send_html(f'<div class="card"><a href="/">← Назад</a><h2>Ошибка</h2><p class="bad">{html.escape(str(e))}</p></div>', 400)
            return
        self.redirect('/')

    def render_index(self):
        cfg = load_config()
        state = load_state()
        rows = []
        for name, sess in sorted(state.get('sessions', {}).items()):
            alive = proc_alive(sess.get('pid'))
            status = '<span class="ok">active</span>' if alive else '<span class="bad">stopped</span>'
            link = read_text(sess.get('link_path',''))
            link_html = html.escape(link) if link else '<span class="muted">ссылка ещё создаётся, обнови страницу через 5–15 секунд</span>'
            created = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(sess.get('created_at', 0)))
            rows.append(f'''
            <tr>
              <td><b>{html.escape(name)}</b><br><span class="muted">PID {html.escape(str(sess.get('pid','')))} · {created}</span></td>
              <td>{status}</td>
              <td><div class="link">{link_html}</div></td>
              <td class="row-actions">
                <a href="/logs/{html.escape(name)}">Логи</a>
                <form method="post" action="/delete" onsubmit="return confirm('Удалить ссылку {html.escape(name)}?')">
                  <input type="hidden" name="name" value="{html.escape(name)}">
                  <button class="danger" type="submit">Удалить</button>
                </form>
              </td>
            </tr>''')
        table = ''.join(rows) or '<tr><td colspan="4" class="muted">Ссылок пока нет.</td></tr>'
        body = f'''
        <div class="top"><h1>Whitelist Bypass Panel</h1><span class="muted">WB Stream only · порт {PORT}</span></div>
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
          <table><thead><tr><th>Имя</th><th>Статус</th><th>Join link</th><th>Действия</th></tr></thead><tbody>{table}</tbody></table>
        </div>
        <div class="card">
          <h2>Android</h2>
          <p>Скопируй join link в приложение <b>whitelist-bypass.apk</b>, нажми Connect/GO и разреши VPN.</p>
          <p class="muted">Creator запускается на сервере; телефон выступает Joiner.</p>
        </div>
        '''
        self.send_html(body)

    def log_message(self, fmt, *args):
        print('%s - - [%s] %s' % (self.address_string(), self.log_date_time_string(), fmt % args))


def main():
    ensure_dirs()
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(f'Config not found: {CONFIG_PATH}')
    if not os.path.exists(CREATOR_BIN):
        raise SystemExit(f'Creator binary not found: {CREATOR_BIN}')
    print(f'wlb-panel listening on http://{HOST}:{PORT}')
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.serve_forever()

if __name__ == '__main__':
    main()
