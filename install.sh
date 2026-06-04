#!/usr/bin/env bash
set -euo pipefail

PORT="8088"
PANEL_DIR="/opt/wlb-panel"
WLB_DIR="/opt/whitelist-bypass"
DATA_DIR="/var/lib/wlb-panel"
LOG_DIR="/var/log/wlb-panel"
CONFIG_DIR="/etc/wlb-panel"
PANEL_USER="wlbpanel"
REPO="kulikov0/whitelist-bypass"
PANEL_URL="https://raw.githubusercontent.com/butuvladislav47-stack/wlb-panel/main/panel.py"
CREATOR_BIN="$WLB_DIR/headless-wbstream-creator"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запусти от root: sudo bash install.sh"
  exit 1
fi

echo "==> Installing dependencies"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates python3 openssl git golang-go

if ! id -u "$PANEL_USER" >/dev/null 2>&1; then
  useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$PANEL_USER"
fi

mkdir -p "$PANEL_DIR" "$WLB_DIR" "$DATA_DIR/sessions" "$LOG_DIR" "$CONFIG_DIR"

echo "==> Installing panel.py"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/panel.py" ]]; then
  cp "$SCRIPT_DIR/panel.py" "$PANEL_DIR/panel.py"
else
  curl -fL "$PANEL_URL" -o "$PANEL_DIR/panel.py"
fi
chmod +x "$PANEL_DIR/panel.py"
python3 -m py_compile "$PANEL_DIR/panel.py"

echo "==> Installing headless-wbstream-creator"
TMP_JSON="$(mktemp)"
URL=""
if curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" -o "$TMP_JSON"; then
  URL="$(python3 - <<'PY' "$TMP_JSON"
import json, re, sys
p=sys.argv[1]
data=json.load(open(p, encoding='utf-8'))
assets=data.get('assets', [])
patterns=[
    r'headless-wbstream-creator.*linux.*(x64|amd64)',
    r'headless-wbstream.*creator.*linux',
    r'wbstream.*creator.*linux.*(x64|amd64)',
]
for pat in patterns:
    for a in assets:
        name=a.get('name','')
        if re.search(pat, name, re.I):
            print(a.get('browser_download_url',''))
            raise SystemExit
print('')
PY
)"
fi
rm -f "$TMP_JSON"

if [[ -n "$URL" ]]; then
  echo "Downloading creator binary: $URL"
  curl -fL "$URL" -o "$CREATOR_BIN"
  chmod +x "$CREATOR_BIN"
else
  echo "No release binary found. Building from source..."
  BUILD_DIR="$(mktemp -d)"
  git clone --depth 1 "https://github.com/${REPO}.git" "$BUILD_DIR"
  cd "$BUILD_DIR"
  if [[ -x ./build-headless.sh ]]; then
    ./build-headless.sh
  fi
  FOUND="$(find "$BUILD_DIR" -type f -name 'headless-wbstream-creator*' -perm /111 | head -n 1 || true)"
  if [[ -z "$FOUND" ]]; then
    echo "Could not find built headless-wbstream-creator. Trying go build search..."
    CANDIDATE_DIR="$(find "$BUILD_DIR" -type f -name '*.go' -path '*wbstream*' -printf '%h\n' | sort -u | head -n 1 || true)"
    if [[ -z "$CANDIDATE_DIR" ]]; then
      echo "ERROR: cannot locate wbstream creator sources"
      exit 1
    fi
    (cd "$CANDIDATE_DIR" && go build -trimpath -ldflags='-s -w' -o "$CREATOR_BIN" .)
  else
    cp "$FOUND" "$CREATOR_BIN"
  fi
  chmod +x "$CREATOR_BIN"
  rm -rf "$BUILD_DIR"
fi

if ! "$CREATOR_BIN" --help >/dev/null 2>&1; then
  echo "WARNING: creator --help returned non-zero; continuing, but check binary manually if sessions fail."
fi

echo "==> Creating config"
PASSWORD="$(openssl rand -base64 18 | tr -d '=+/ ' | cut -c1-16)"
python3 - <<PY
import json, hashlib, secrets, os
path = "$CONFIG_DIR/config.json"
password = "$PASSWORD"
salt = secrets.token_hex(16)
dk = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 200000).hex()
cfg = {'username': 'admin', 'password': {'scheme':'pbkdf2_sha256','salt':salt,'hash':dk}}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.chmod(path, 0o600)
PY

echo "==> Writing systemd service"
cat > /etc/systemd/system/wlb-panel.service <<EOF
[Unit]
Description=Whitelist Bypass Web Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$PANEL_USER
Group=$PANEL_USER
Environment=WLB_PANEL_PORT=$PORT
Environment=WLB_PANEL_CONFIG=$CONFIG_DIR/config.json
Environment=WLB_PANEL_DATA=$DATA_DIR
Environment=WLB_PANEL_LOG=$LOG_DIR
Environment=WLB_CREATOR_BIN=$CREATOR_BIN
Environment=WLB_COOKIES=$CONFIG_DIR/wb-cookies.json
Environment=WLB_RESOURCES=moderate
ExecStart=/usr/bin/python3 $PANEL_DIR/panel.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

chown -R "$PANEL_USER:$PANEL_USER" "$PANEL_DIR" "$WLB_DIR" "$DATA_DIR" "$LOG_DIR" "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
chmod 755 "$PANEL_DIR" "$WLB_DIR"

systemctl daemon-reload
systemctl enable wlb-panel >/dev/null
systemctl restart wlb-panel

SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [[ -z "${SERVER_IP:-}" ]]; then SERVER_IP="SERVER_IP"; fi

cat <<EOF

============================================================
WLB Panel v2 installed.

URL:      http://${SERVER_IP}:${PORT}
Login:    admin
Password: ${PASSWORD}

What changed in v2:
  - Settings page added
  - Change password in web panel
  - WB Stream cookies upload/save in web panel
  - Creator starts with --cookies when cookies are configured

Commands:
  systemctl status wlb-panel
  journalctl -u wlb-panel -f
============================================================

EOF
