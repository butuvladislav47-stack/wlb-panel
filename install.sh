#!/usr/bin/env bash
set -Eeuo pipefail

VERSION="alpha 0.0.1"
PORT="8088"
NOVNC_PORT="6080"
PANEL_DIR="/opt/wlb-panel"
WB_DIR="/opt/whitelist-bypass"
STATE_DIR="/var/lib/wlb-panel"
LOG_DIR="/var/log/wlb-panel"
CONF_DIR="/etc/wlb-panel"
USER_NAME="wlbpanel"
REPO_RAW_BASE="https://raw.githubusercontent.com/butuvladislav47-stack/wlb-panel/main"
PANEL_URL="${REPO_RAW_BASE}/panel.py"
WB_REPO="https://github.com/kulikov0/whitelist-bypass.git"

log(){ echo -e "\033[1;34m[wlb]\033[0m $*"; }
warn(){ echo -e "\033[1;33m[warn]\033[0m $*"; }
fail(){ echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

trap 'fail "Installation failed on line $LINENO. Last command: $BASH_COMMAND"' ERR

if [[ "${EUID}" -ne 0 ]]; then
  fail "Run as root: sudo bash install.sh"
fi

export DEBIAN_FRONTEND=noninteractive

log "Installing WLB Panel ${VERSION}..."

log "Stopping old service and browser processes if present..."
systemctl stop wlb-panel 2>/dev/null || true
pkill -f '/opt/wlb-panel/panel.py' 2>/dev/null || true
pkill -f 'headless-wbstream-creator' 2>/dev/null || true
pkill -f 'websockify.*6080' 2>/dev/null || true
pkill -f 'x11vnc.*5901' 2>/dev/null || true
pkill -f 'Xvfb :99' 2>/dev/null || true
pkill -f 'google-chrome.*stream.wb.ru' 2>/dev/null || true
pkill -f 'chromium.*stream.wb.ru' 2>/dev/null || true

log "Updating apt indexes..."
apt-get update -y

log "Installing base dependencies for Ubuntu 24.04..."
apt-get install -y --no-install-recommends \
  ca-certificates curl wget gnupg gpg unzip jq openssl \
  python3 python3-minimal \
  git golang-go \
  xvfb x11vnc websockify novnc openbox xauth dbus-x11 \
  fonts-liberation fonts-liberation-sans-narrow \
  libnss3 libatk-bridge2.0-0t64 libgtk-3-0t64 libgbm1 libasound2t64 \
  libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 libxss1 libxtst6 \
  xdg-utils >/dev/null

command -v go >/dev/null || fail "Go was not installed. Check apt repository availability."
command -v git >/dev/null || fail "Git was not installed."
command -v Xvfb >/dev/null || fail "Xvfb was not installed."
command -v x11vnc >/dev/null || fail "x11vnc was not installed."
command -v websockify >/dev/null || fail "websockify was not installed."
command -v openbox >/dev/null || fail "openbox was not installed."

log "Installing Google Chrome stable if needed..."
if ! command -v google-chrome >/dev/null && ! command -v google-chrome-stable >/dev/null; then
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-linux-signing-keyring.gpg
  chmod a+r /etc/apt/keyrings/google-linux-signing-keyring.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-linux-signing-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
  apt-get update -y
  apt-get install -y google-chrome-stable || warn "google-chrome-stable install failed; will try chromium fallback."
fi

if ! command -v google-chrome >/dev/null && ! command -v google-chrome-stable >/dev/null; then
  warn "Google Chrome not found; trying chromium-browser fallback."
  apt-get install -y chromium-browser || true
fi

if ! command -v google-chrome >/dev/null && ! command -v google-chrome-stable >/dev/null && ! command -v chromium-browser >/dev/null && ! command -v chromium >/dev/null; then
  fail "No Chrome/Chromium executable found after installation."
fi

log "Creating system user and directories..."
id -u "${USER_NAME}" >/dev/null 2>&1 || useradd --system --home "${STATE_DIR}" --shell /usr/sbin/nologin "${USER_NAME}"
mkdir -p "${PANEL_DIR}" "${WB_DIR}" "${STATE_DIR}/sessions" "${STATE_DIR}/chrome-profile" "${STATE_DIR}/runtime" "${LOG_DIR}" "${CONF_DIR}"
chown -R "${USER_NAME}:${USER_NAME}" "${STATE_DIR}" "${LOG_DIR}" "${CONF_DIR}"
chmod 700 "${CONF_DIR}"
chmod 700 "${STATE_DIR}/runtime"

log "Downloading panel.py..."
curl -fsSL "${PANEL_URL}" -o "${PANEL_DIR}/panel.py"
chmod 755 "${PANEL_DIR}/panel.py"
python3 -m py_compile "${PANEL_DIR}/panel.py"

log "Installing headless-wbstream-creator..."
CREATOR_TARGET="${WB_DIR}/headless-wbstream-creator"
rm -f "${CREATOR_TARGET}"

# Try a few possible release asset names first. If they do not exist, build from source.
ASSET_URLS=(
  "https://github.com/kulikov0/whitelist-bypass/releases/latest/download/headless-wbstream-creator-linux-x64"
  "https://github.com/kulikov0/whitelist-bypass/releases/latest/download/headless-wbstream-creator-linux-amd64"
  "https://github.com/kulikov0/whitelist-bypass/releases/latest/download/headless-wbstream-creator"
)
for u in "${ASSET_URLS[@]}"; do
  if curl -fL --connect-timeout 10 --max-time 60 "$u" -o "${CREATOR_TARGET}.tmp"; then
    if [[ -s "${CREATOR_TARGET}.tmp" ]]; then
      mv "${CREATOR_TARGET}.tmp" "${CREATOR_TARGET}"
      break
    fi
  fi
  rm -f "${CREATOR_TARGET}.tmp"
done

if [[ ! -s "${CREATOR_TARGET}" ]]; then
  log "Prebuilt creator not found. Building whitelist-bypass from source..."
  rm -rf /tmp/whitelist-bypass-src
  git clone --depth 1 "${WB_REPO}" /tmp/whitelist-bypass-src
  cd /tmp/whitelist-bypass-src
  chmod +x ./build-headless.sh || true
  ./build-headless.sh
  FOUND_CREATOR="$(find /tmp/whitelist-bypass-src -type f -name 'headless-wbstream-creator' -perm /111 | head -n 1 || true)"
  [[ -n "${FOUND_CREATOR}" ]] || fail "Build finished, but headless-wbstream-creator was not found."
  cp "${FOUND_CREATOR}" "${CREATOR_TARGET}"
fi

chmod 755 "${CREATOR_TARGET}"
"${CREATOR_TARGET}" --help >/dev/null 2>&1 || warn "Creator exists but --help returned non-zero; continuing."
ls -lh "${CREATOR_TARGET}"

log "Creating initial config..."
PASSWORD="$(openssl rand -base64 18 | tr -d '=+/ ' | cut -c1-16)"
PASS_HASH="$(python3 - <<PY
import hashlib
print(hashlib.sha256('${PASSWORD}'.encode()).hexdigest())
PY
)"
cat > "${CONF_DIR}/config.json" <<JSON
{
  "username": "admin",
  "password_sha256": "${PASS_HASH}"
}
JSON
chown -R "${USER_NAME}:${USER_NAME}" "${CONF_DIR}" "${STATE_DIR}" "${LOG_DIR}"
chmod 600 "${CONF_DIR}/config.json"

log "Creating systemd service..."
cat > /etc/systemd/system/wlb-panel.service <<EOF
[Unit]
Description=WLB Panel ${VERSION}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER_NAME}
Group=${USER_NAME}
Environment=WLB_PORT=${PORT}
WorkingDirectory=${PANEL_DIR}
ExecStart=/usr/bin/python3 ${PANEL_DIR}/panel.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wlb-panel >/dev/null
systemctl restart wlb-panel
sleep 2
systemctl is-active --quiet wlb-panel || { journalctl -u wlb-panel -n 80 --no-pager; fail "wlb-panel service is not active."; }

log "Opening UFW ports if UFW is installed..."
if command -v ufw >/dev/null 2>&1; then
  ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true
  ufw allow "${NOVNC_PORT}/tcp" >/dev/null 2>&1 || true
fi

SERVER_IP="$(hostname -I | awk '{print $1}')"
cat <<EOF
============================================================
WLB Panel release ${VERSION} installed.

URL:      http://${SERVER_IP}:${PORT}
Login:    admin
Password: ${PASSWORD}

What is included:
  - Web panel on port ${PORT}
  - Server-side Chrome/noVNC on port ${NOVNC_PORT}
  - Real cookie import from server Chrome via DevTools
  - Create/delete named WB Stream links
  - Change password in web panel
  - Autostart with systemd

Open ports at your hosting firewall if needed:
  ${PORT}/tcp for panel
  ${NOVNC_PORT}/tcp for embedded browser/noVNC

Commands:
  systemctl status wlb-panel
  journalctl -u wlb-panel -f
============================================================
EOF
