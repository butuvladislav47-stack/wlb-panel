#!/usr/bin/env bash
set -euo pipefail

PORT="8088"
NOVNC_PORT="6080"
PANEL_DIR="/opt/wlb-panel"
WLB_DIR="/opt/whitelist-bypass"
DATA_DIR="/var/lib/wlb-panel"
LOG_DIR="/var/log/wlb-panel"
ETC_DIR="/etc/wlb-panel"
PANEL_USER="wlbpanel"
REPO_RAW_BASE="https://raw.githubusercontent.com/butuvladislav47-stack/wlb-panel/main"
PANEL_URL="$REPO_RAW_BASE/panel.py"

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo bash install.sh" >&2
    exit 1
  fi
}

rand_pass() {
  tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16
}

sha256_py() {
  python3 - "$1" <<'PY'
import hashlib, sys
print(hashlib.sha256(sys.argv[1].encode()).hexdigest())
PY
}

install_google_chrome() {
  if command -v google-chrome >/dev/null 2>&1 || command -v google-chrome-stable >/dev/null 2>&1; then
    return 0
  fi
  echo "Installing Google Chrome..."
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-linux.gpg || true
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
  apt-get update || true
  apt-get install -y google-chrome-stable || true
  if ! command -v google-chrome >/dev/null 2>&1 && ! command -v google-chrome-stable >/dev/null 2>&1; then
    echo "Google Chrome install failed; trying chromium-browser..."
    apt-get install -y chromium-browser || apt-get install -y chromium || true
  fi
}

download_creator() {
  mkdir -p "$WLB_DIR"
  local target="$WLB_DIR/headless-wbstream-creator"
  echo "Installing headless-wbstream-creator..."
  local urls=(
    "https://github.com/kulikov0/whitelist-bypass/releases/latest/download/headless-wbstream-creator-linux-x64"
    "https://github.com/kulikov0/whitelist-bypass/releases/latest/download/headless-wbstream-creator-linux-amd64"
    "https://github.com/kulikov0/whitelist-bypass/releases/latest/download/whitelist-bypass-headless-wbstream-creator-linux-x64"
  )
  for u in "${urls[@]}"; do
    if curl -fL --connect-timeout 20 -o "$target" "$u"; then
      chmod +x "$target"
      if "$target" --help >/dev/null 2>&1 || "$target" -h >/dev/null 2>&1; then
        echo "Downloaded creator from $u"
        return 0
      fi
    fi
  done

  echo "Prebuilt creator not found. Building from source..."
  apt-get install -y git golang-go
  rm -rf /tmp/whitelist-bypass-src
  git clone --depth=1 https://github.com/kulikov0/whitelist-bypass.git /tmp/whitelist-bypass-src
  cd /tmp/whitelist-bypass-src
  if [[ -x ./build-headless.sh ]]; then
    ./build-headless.sh || true
  fi
  local found
  found="$(find /tmp/whitelist-bypass-src -type f -name 'headless-wbstream-creator' -perm -111 | head -n 1 || true)"
  if [[ -z "$found" ]]; then
    found="$(find /tmp/whitelist-bypass-src -type f -name '*wbstream*creator*' -perm -111 | head -n 1 || true)"
  fi
  if [[ -z "$found" ]]; then
    echo "Could not build/find headless-wbstream-creator" >&2
    exit 1
  fi
  cp "$found" "$target"
  chmod +x "$target"
}

need_root
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl ca-certificates python3 openssl jq unzip gpg xvfb x11vnc websockify novnc openbox dbus-x11 xauth fonts-liberation libnss3 libatk-bridge2.0-0 libgtk-3-0 libgbm1 libasound2t64 || true
install_google_chrome

if ! id "$PANEL_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/wlb-panel --shell /usr/sbin/nologin "$PANEL_USER"
fi

mkdir -p "$PANEL_DIR" "$WLB_DIR" "$DATA_DIR/sessions" "$DATA_DIR/browser-profile" "$LOG_DIR" "$ETC_DIR"

curl -fL -o "$PANEL_DIR/panel.py" "$PANEL_URL"
chmod +x "$PANEL_DIR/panel.py"
python3 -m py_compile "$PANEL_DIR/panel.py"

download_creator

PASS="$(rand_pass)"
HASH="$(sha256_py "$PASS")"
cat > "$ETC_DIR/config.json" <<JSON
{
  "username": "admin",
  "password_sha256": "$HASH",
  "session_token": ""
}
JSON

chown -R "$PANEL_USER:$PANEL_USER" "$PANEL_DIR" "$DATA_DIR" "$LOG_DIR" "$ETC_DIR"
chown -R "$PANEL_USER:$PANEL_USER" "$WLB_DIR"
chmod 700 "$ETC_DIR"
chmod 600 "$ETC_DIR/config.json"

cat > /etc/systemd/system/wlb-panel.service <<EOF
[Unit]
Description=WLB Panel v5
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$PANEL_USER
Group=$PANEL_USER
Environment=WLB_PANEL_PORT=$PORT
Environment=WLB_NOVNC_PORT=$NOVNC_PORT
WorkingDirectory=$PANEL_DIR
ExecStart=/usr/bin/python3 $PANEL_DIR/panel.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wlb-panel >/dev/null
systemctl restart wlb-panel

IP="$(hostname -I | awk '{print $1}')"
cat <<EOF
============================================================
WLB Panel v5 installed.

URL:      http://${IP}:${PORT}
Login:    admin
Password: ${PASS}

What is new in v5:
  - Redesigned UI
  - Fixed config permissions
  - Better WB Login Browser startup
  - Google Chrome install fallback
  - Clear cookies status and logs
  - Create/delete named WB Stream links
  - Change password in web panel

Open ports if needed:
  ${PORT}/tcp for panel
  ${NOVNC_PORT}/tcp for embedded browser/noVNC

Install command:
  sudo bash -c "\$(wget -qO- ${REPO_RAW_BASE}/install.sh)"

Commands:
  systemctl status wlb-panel
  journalctl -u wlb-panel -f
  tail -n 200 ${LOG_DIR}/browser.log
============================================================
EOF
