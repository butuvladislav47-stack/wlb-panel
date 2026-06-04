#!/usr/bin/env bash
set -euo pipefail

VERSION="v6"
PORT="8088"
NOVNC_PORT="6080"
PANEL_DIR="/opt/wlb-panel"
WB_DIR="/opt/whitelist-bypass"
STATE_DIR="/var/lib/wlb-panel"
LOG_DIR="/var/log/wlb-panel"
CONFIG_DIR="/etc/wlb-panel"
RUN_USER="wlbpanel"
RAW_BASE="https://raw.githubusercontent.com/butuvladislav47-stack/wlb-panel/main"
PANEL_URL="${RAW_BASE}/panel.py"
WLB_REPO="https://github.com/kulikov0/whitelist-bypass.git"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash install.sh"
  exit 1
fi

echo "Installing WLB Panel ${VERSION}..."

apt-get update
apt-get install -y curl ca-certificates python3 openssl unzip gpg jq git golang-go \
  xvfb x11vnc websockify novnc openbox xauth dbus-x11 fonts-liberation \
  libnss3 libatk-bridge2.0-0 libgtk-3-0 libgbm1 libasound2 || true

# Install Chrome if possible. Fallback to Ubuntu chromium-browser if available.
if ! command -v google-chrome >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
  echo "Installing Google Chrome..."
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-linux.gpg || true
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
  apt-get update || true
  apt-get install -y google-chrome-stable || apt-get install -y chromium-browser || true
fi

if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  useradd --system --home "$STATE_DIR" --shell /usr/sbin/nologin "$RUN_USER"
fi

mkdir -p "$PANEL_DIR" "$WB_DIR" "$STATE_DIR/sessions" "$LOG_DIR" "$CONFIG_DIR" "$STATE_DIR/chrome-profile"
chown -R "$RUN_USER:$RUN_USER" "$STATE_DIR" "$LOG_DIR" "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"

# Download panel
curl -fsSL "$PANEL_URL" -o "$PANEL_DIR/panel.py"
chmod +x "$PANEL_DIR/panel.py"
python3 -m py_compile "$PANEL_DIR/panel.py"

# Install headless-wbstream-creator robustly.
echo "Installing headless-wbstream-creator..."
CREATOR="$WB_DIR/headless-wbstream-creator"
rm -f "$CREATOR"

# Try several possible release asset names, but do not fail if missing.
ASSETS=(
  "headless-wbstream-creator-linux-x64"
  "headless-wbstream-creator-linux-amd64"
  "headless-wbstream-creator"
)
for asset in "${ASSETS[@]}"; do
  url="https://github.com/kulikov0/whitelist-bypass/releases/latest/download/${asset}"
  echo "Trying prebuilt: $url"
  if curl -fL --connect-timeout 15 --max-time 120 "$url" -o "$CREATOR"; then
    chmod +x "$CREATOR"
    if "$CREATOR" --help >/dev/null 2>&1 || file "$CREATOR" | grep -qi 'executable'; then
      echo "Prebuilt creator installed: $asset"
      break
    fi
  fi
  rm -f "$CREATOR"
done

if [[ ! -x "$CREATOR" ]]; then
  echo "Prebuilt creator not found. Building from source..."
  rm -rf /tmp/whitelist-bypass-src
  git clone --depth=1 "$WLB_REPO" /tmp/whitelist-bypass-src
  cd /tmp/whitelist-bypass-src
  if [[ -x ./build-headless.sh ]]; then
    bash ./build-headless.sh
  else
    echo "build-headless.sh not found, trying direct go build..."
    cd /tmp/whitelist-bypass-src/headless/wbstream
    go build -trimpath -ldflags="-s -w" -o headless-wbstream-creator .
  fi

  echo "Searching built headless-wbstream-creator..."
  BUILT=""
  while IFS= read -r f; do
    if [[ -x "$f" ]] && file "$f" | grep -qi 'executable'; then
      BUILT="$f"
      break
    fi
  done < <(find /tmp/whitelist-bypass-src -type f -name 'headless-wbstream-creator' -print)

  if [[ -z "$BUILT" ]]; then
    echo "ERROR: build completed but headless-wbstream-creator was not found."
    find /tmp/whitelist-bypass-src -type f -name '*wbstream*' -maxdepth 5 -ls || true
    exit 20
  fi

  echo "Built creator found: $BUILT"
  install -m 0755 "$BUILT" "$CREATOR"
fi

if [[ ! -x "$CREATOR" ]]; then
  echo "ERROR: $CREATOR is missing or not executable"
  exit 21
fi

echo "Creator installed:"
ls -lh "$CREATOR"

# Config
PASS="$(openssl rand -base64 18 | tr -d '=+/ ' | cut -c1-16)"
HASH="$(python3 - <<PY
import hashlib
print(hashlib.sha256("$PASS".encode()).hexdigest())
PY
)"
cat > "$CONFIG_DIR/config.json" <<JSON
{
  "username": "admin",
  "password_sha256": "$HASH",
  "session_token": "",
  "version": "$VERSION",
  "port": $PORT,
  "novnc_port": $NOVNC_PORT,
  "creator_path": "$CREATOR",
  "cookies_path": "$CONFIG_DIR/wb-cookies.json",
  "chrome_profile": "$STATE_DIR/chrome-profile"
}
JSON
chown -R "$RUN_USER:$RUN_USER" "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR"
chmod 640 "$CONFIG_DIR/config.json"

cat > /etc/systemd/system/wlb-panel.service <<SERVICE
[Unit]
Description=WLB Panel v6
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$PANEL_DIR
ExecStart=/usr/bin/python3 $PANEL_DIR/panel.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable wlb-panel >/dev/null
systemctl restart wlb-panel
sleep 1

if ! systemctl is-active --quiet wlb-panel; then
  echo "ERROR: wlb-panel service failed to start. Logs:"
  journalctl -u wlb-panel -n 80 --no-pager || true
  exit 30
fi

IP="$(hostname -I | awk '{print $1}')"
cat <<OUT
============================================================
WLB Panel v6 installed.

URL:      http://${IP}:${PORT}
Login:    admin
Password: ${PASS}

What is fixed in v6:
  - Robust headless-wbstream-creator install/build/copy check
  - Clear installation errors if creator is missing
  - Fixed config permissions for login/cookies saving
  - Improved browser start page: https://stream.wb.ru/login
  - v5 UI features preserved

Open ports if needed:
  ${PORT}/tcp for panel
  ${NOVNC_PORT}/tcp for embedded browser/noVNC

Commands:
  systemctl status wlb-panel
  journalctl -u wlb-panel -f
============================================================
OUT
