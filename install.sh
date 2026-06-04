#!/usr/bin/env bash
set -euo pipefail

PORT="8088"
PANEL_DIR="/opt/wlb-panel"
WLB_DIR="/opt/whitelist-bypass"
CONFIG_DIR="/etc/wlb-panel"
DATA_DIR="/var/lib/wlb-panel"
LOG_DIR="/var/log/wlb-panel"
PANEL_URL="https://raw.githubusercontent.com/butuvladislav47-stack/wlb-panel/main/panel.py"
CREATOR_REPO="kulikov0/whitelist-bypass"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash install.sh"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "[1/9] Installing packages..."
apt-get update
apt-get install -y curl wget ca-certificates openssl python3 git golang-go unzip procps \
  xvfb x11vnc novnc websockify openbox fonts-liberation libasound2t64 \
  libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 \
  libgtk-3-0 libnspr4 libnss3 libxcomposite1 libxdamage1 libxrandr2 xdg-utils || true

# Ubuntu 24.04 often provides Chromium as snap, which is unreliable for this system service.
# Install Google Chrome .deb so the server browser opens directly inside noVNC.
if ! command -v google-chrome-stable >/dev/null 2>&1; then
  echo "Installing google-chrome-stable..."
  curl -fL -o /tmp/google-chrome-stable_current_amd64.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y /tmp/google-chrome-stable_current_amd64.deb || true
fi

if ! id wlbpanel >/dev/null 2>&1; then
  useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin wlbpanel
fi

mkdir -p "$PANEL_DIR" "$WLB_DIR" "$CONFIG_DIR" "$DATA_DIR/sessions" "$DATA_DIR/chrome-profile" "$LOG_DIR"
chown -R wlbpanel:wlbpanel "$DATA_DIR" "$LOG_DIR"
chmod 700 "$CONFIG_DIR"

echo "[2/9] Installing panel.py..."
curl -fsSL "$PANEL_URL" -o "$PANEL_DIR/panel.py"
chmod +x "$PANEL_DIR/panel.py"
python3 -m py_compile "$PANEL_DIR/panel.py"

find_asset_url() {
  local pattern="$1"
  curl -fsSL "https://api.github.com/repos/${CREATOR_REPO}/releases/latest" | python3 - "$pattern" <<'PY'
import json,sys,re
pat=sys.argv[1]
data=json.load(sys.stdin)
for a in data.get('assets',[]):
    name=a.get('name','')
    if re.search(pat,name,re.I):
        print(a.get('browser_download_url',''))
        sys.exit(0)
sys.exit(1)
PY
}

echo "[3/9] Installing headless-wbstream-creator..."
CREATOR_BIN="$WLB_DIR/headless-wbstream-creator"
ASSET_URL=""
for pat in 'headless.*wbstream.*creator.*linux.*(x64|amd64)' 'wbstream.*creator.*linux.*(x64|amd64)' 'linux.*(x64|amd64).*headless.*wbstream.*creator'; do
  if ASSET_URL=$(find_asset_url "$pat" 2>/dev/null); then
    [[ -n "$ASSET_URL" ]] && break
  fi
  ASSET_URL=""
done

if [[ -n "$ASSET_URL" ]]; then
  echo "Downloading: $ASSET_URL"
  curl -fL "$ASSET_URL" -o "$CREATOR_BIN"
  chmod +x "$CREATOR_BIN"
else
  echo "Binary asset not found. Building from source..."
  TMP="$(mktemp -d)"
  git clone --depth=1 "https://github.com/${CREATOR_REPO}.git" "$TMP/src"
  cd "$TMP/src"
  if [[ -d headless/wbstream ]]; then
    (cd headless/wbstream && go build -trimpath -ldflags='-s -w' -o "$CREATOR_BIN" .)
  else
    echo "Cannot find headless/wbstream in source tree"
    exit 1
  fi
  chmod +x "$CREATOR_BIN"
  rm -rf "$TMP"
fi

if [[ ! -x "$CREATOR_BIN" ]]; then
  echo "headless-wbstream-creator install failed"
  exit 1
fi

if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
  echo "[4/9] Generating admin password..."
  PASS="$(openssl rand -base64 18 | tr -d '=+/ ' | cut -c1-16)"
  HASH="$(python3 - <<PY
import hashlib
print(hashlib.sha256('$PASS'.encode()).hexdigest())
PY
)"
  cat > "$CONFIG_DIR/config.json" <<JSON
{
  "username": "admin",
  "password_sha256": "$HASH",
  "session_token": ""
}
JSON
  chmod 600 "$CONFIG_DIR/config.json"
else
  PASS="(existing password kept)"
fi

chown -R wlbpanel:wlbpanel "$PANEL_DIR" "$WLB_DIR" "$DATA_DIR" "$LOG_DIR"
chown -R wlbpanel:wlbpanel "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
chmod 640 "$CONFIG_DIR/config.json"

# Allow wlbpanel to write cookie file in /etc/wlb-panel
if [[ ! -f "$CONFIG_DIR/wb-cookies.json" ]]; then
  echo "[]" > "$CONFIG_DIR/wb-cookies.json"
fi
chown wlbpanel:wlbpanel "$CONFIG_DIR/wb-cookies.json"
chmod 660 "$CONFIG_DIR/wb-cookies.json"

echo "[5/9] Creating systemd service..."
cat > /etc/systemd/system/wlb-panel.service <<EOF
[Unit]
Description=WLB Panel v3
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=wlbpanel
Group=wlbpanel
Environment=WLB_PANEL_PORT=${PORT}
ExecStart=/usr/bin/python3 ${PANEL_DIR}/panel.py
Restart=always
RestartSec=3
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wlb-panel >/dev/null
systemctl restart wlb-panel
sleep 1

IP="$(curl -fsS --max-time 3 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"

echo
cat <<EOF
============================================================
WLB Panel v3 installed.

URL:      http://${IP}:${PORT}
Login:    admin
Password: ${PASS}

What is new in v3:
  - WB Login Browser inside panel via noVNC on port 6080
  - Import WB cookies from server-side Chromium
  - Change password in web panel
  - Create/delete named WB Stream links

Open ports if needed:
  ${PORT}/tcp for panel
  6080/tcp for embedded browser/noVNC

Commands:
  systemctl status wlb-panel
  journalctl -u wlb-panel -f
============================================================
EOF
