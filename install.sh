#!/usr/bin/env bash
set -Eeuo pipefail

VERSION="wlb2 server 0.1.0"
PORT="8088"
NOVNC_PORT="6080"
PANEL_DIR="/opt/wlb2-panel"
WB_DIR="/opt/whitelist-bypass"
STATE_DIR="/var/lib/wlb2-panel"
LOG_DIR="/var/log/wlb2-panel"
CONF_DIR="/etc/wlb2-panel"
USER_NAME="wlbpanel"
SERVICE_NAME="wlb2-panel"
REPO_RAW_BASE="https://raw.githubusercontent.com/butuvladislav47-stack/wlb-panel/main"
PANEL_URL="${REPO_RAW_BASE}/panel.py"
WB_REPO="https://github.com/kulikov0/whitelist-bypass.git"
WB_RELEASE_TAG="v0.3.5"

log(){ echo -e "\033[1;34m[wlb2]\033[0m $*"; }
warn(){ echo -e "\033[1;33m[warn]\033[0m $*"; }
fail(){ echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

trap 'fail "Installation failed on line $LINENO. Last command: $BASH_COMMAND"' ERR

if [[ "${EUID}" -ne 0 ]]; then
  fail "Run as root: sudo bash install.sh"
fi

export DEBIAN_FRONTEND=noninteractive

log "Installing ${VERSION}..."

log "Stopping old ${SERVICE_NAME} service if present..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
if id -u "${USER_NAME}" >/dev/null 2>&1; then
  pkill -TERM -u "${USER_NAME}" 2>/dev/null || true
  sleep 1
  pkill -KILL -u "${USER_NAME}" 2>/dev/null || true
fi

log "Updating apt indexes..."
apt-get update -y

log "Installing dependencies..."
BASE_PACKAGES=(
  ca-certificates curl wget gnupg gpg unzip jq openssl \
  python3 python3-minimal \
  git golang-go \
  xvfb x11vnc websockify novnc openbox xauth dbus-x11 \
  fonts-liberation fonts-liberation-sans-narrow \
  libnss3 libgbm1 libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 \
  libxss1 libxtst6 xdg-utils
)
apt-get install -y --no-install-recommends "${BASE_PACKAGES[@]}"

# Ubuntu 24.04 uses t64 package names. Older Ubuntu releases use old names.
apt-get install -y --no-install-recommends libatk-bridge2.0-0t64 libgtk-3-0t64 libasound2t64 2>/dev/null \
  || apt-get install -y --no-install-recommends libatk-bridge2.0-0 libgtk-3-0 libasound2

command -v go >/dev/null || fail "Go was not installed."
command -v git >/dev/null || fail "Git was not installed."
command -v Xvfb >/dev/null || fail "Xvfb was not installed."
command -v x11vnc >/dev/null || fail "x11vnc was not installed."
command -v websockify >/dev/null || fail "websockify was not installed."
command -v openbox >/dev/null || fail "openbox was not installed."

log "Installing Google Chrome stable if needed..."
if ! command -v google-chrome >/dev/null && ! command -v google-chrome-stable >/dev/null; then
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor --yes -o /etc/apt/keyrings/google-linux-signing-keyring.gpg
  chmod a+r /etc/apt/keyrings/google-linux-signing-keyring.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-linux-signing-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
  apt-get update -y
  apt-get install -y google-chrome-stable || warn "google-chrome-stable install failed; trying Chromium fallback."
fi

if ! command -v google-chrome >/dev/null && ! command -v google-chrome-stable >/dev/null; then
  apt-get install -y chromium-browser || apt-get install -y chromium || true
fi

if ! command -v google-chrome >/dev/null && ! command -v google-chrome-stable >/dev/null && ! command -v chromium-browser >/dev/null && ! command -v chromium >/dev/null; then
  fail "No Chrome/Chromium executable found after installation."
fi

log "Creating system user and directories..."
id -u "${USER_NAME}" >/dev/null 2>&1 || useradd --system --home "${STATE_DIR}" --shell /usr/sbin/nologin "${USER_NAME}"
mkdir -p "${PANEL_DIR}" "${WB_DIR}" "${STATE_DIR}/sessions" "${STATE_DIR}/chrome-profile" "${STATE_DIR}/runtime" "${LOG_DIR}" "${CONF_DIR}"

# Temporary WB Stream rooms cannot safely survive reboot/reinstall. Permanent
# clients and WB cookies are preserved; active rooms are recreated on demand.
rm -rf "${STATE_DIR}/sessions"
mkdir -p "${STATE_DIR}/sessions"

log "Installing panel.py..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/panel.py" ]]; then
  cp "${SCRIPT_DIR}/panel.py" "${PANEL_DIR}/panel.py"
else
  curl -fsSL "${PANEL_URL}" -o "${PANEL_DIR}/panel.py"
fi
chmod 755 "${PANEL_DIR}/panel.py"
python3 -m py_compile "${PANEL_DIR}/panel.py"

log "Building headless-wbstream-creator with server-side DNS redirect..."
CREATOR_TARGET="${WB_DIR}/headless-wbstream-creator"
BUILD_DIR="/tmp/wlb2-whitelist-bypass-src"
rm -rf "${BUILD_DIR}"
git clone --depth 1 --branch "${WB_RELEASE_TAG}" "${WB_REPO}" "${BUILD_DIR}"

python3 - "${BUILD_DIR}/relay/tunnel/relay_bridge.go" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
old = '''\taddr := string(payload[1 : 1+addrLen])
\tdata := payload[1+addrLen:]
'''
new = '''\taddr := string(payload[1 : 1+addrLen])
\tdata := payload[1+addrLen:]
\tif _, port, err := net.SplitHostPort(addr); err == nil && port == "53" && addr != "1.1.1.1:53" {
\t\trb.logFn("relay[creator]: redirect DNS %s -> 1.1.1.1:53", common.MaskAddr(addr))
\t\taddr = "1.1.1.1:53"
\t}
'''
if old not in text:
    raise SystemExit("Could not apply server-side DNS redirect patch")
path.write_text(text.replace(old, new, 1))
PY

go -C "${BUILD_DIR}/headless/wbstream" build -trimpath -ldflags="-s -w" -o "${CREATOR_TARGET}" .
rm -rf "${BUILD_DIR}"
chmod 755 "${CREATOR_TARGET}"
"${CREATOR_TARGET}" --help >/dev/null 2>&1 || warn "Creator exists but --help returned non-zero; continuing."
ls -lh "${CREATOR_TARGET}"

log "Creating or keeping panel config..."
PASSWORD=""
if [[ ! -s "${CONF_DIR}/config.json" ]] || ! jq -e '.username | type == "string" and length > 0' "${CONF_DIR}/config.json" >/dev/null 2>&1 \
  || ! jq -e '.password_sha256 | type == "string" and length == 64' "${CONF_DIR}/config.json" >/dev/null 2>&1; then
  PASSWORD="$(openssl rand -base64 18 | tr -d '=+/ ' | cut -c1-16)"
  PASS_HASH="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "${PASSWORD}")"
  cat > "${CONF_DIR}/config.json" <<JSON
{
  "username": "admin",
  "password_sha256": "${PASS_HASH}"
}
JSON
else
  log "Keeping existing panel login and password."
fi

SERVER_IP="$(hostname -I | awk '{print $1}')"
python3 - "${CONF_DIR}/config.json" "http://${SERVER_IP}:${PORT}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
default_url = sys.argv[2]
data = json.loads(path.read_text())
if not data.get("public_base_url"):
    data["public_base_url"] = default_url
path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n")
PY

log "Creating or keeping noVNC password..."
if [[ ! -s "${CONF_DIR}/vnc.pass" ]] || [[ ! -s "${CONF_DIR}/vnc-password.txt" ]]; then
  VNC_PASSWORD="$(openssl rand -base64 18 | tr -d '=+/ ' | cut -c1-8)"
  x11vnc -storepasswd "${VNC_PASSWORD}" "${CONF_DIR}/vnc.pass" >/dev/null
  printf '%s\n' "${VNC_PASSWORD}" > "${CONF_DIR}/vnc-password.txt"
fi

chown -R "${USER_NAME}:${USER_NAME}" "${STATE_DIR}" "${LOG_DIR}" "${CONF_DIR}"
chown -R root:root "${PANEL_DIR}" "${WB_DIR}"
chmod 700 "${CONF_DIR}" "${STATE_DIR}/runtime"
chmod 600 "${CONF_DIR}/config.json"
chmod 600 "${CONF_DIR}/vnc.pass" "${CONF_DIR}/vnc-password.txt"

log "Creating systemd service..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=WLB2 Panel ${VERSION}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER_NAME}
Group=${USER_NAME}
Environment=WLB_PORT=${PORT}
Environment=WLB_BASE=${STATE_DIR}
Environment=WLB_LOGS=${LOG_DIR}
Environment=WLB_CONFIG_DIR=${CONF_DIR}
Environment=WLB_CONFIG=${CONF_DIR}/config.json
Environment=WLB_COOKIES=${CONF_DIR}/wb-cookies.json
Environment=WLB_CREATOR=${CREATOR_TARGET}
Environment=WLB_VNC_PASSWORD_FILE=${CONF_DIR}/vnc.pass
WorkingDirectory=${PANEL_DIR}
ExecStart=/usr/bin/python3 ${PANEL_DIR}/panel.py
Restart=always
RestartSec=3
KillMode=process
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null
systemctl restart "${SERVICE_NAME}"
sleep 2
systemctl is-active --quiet "${SERVICE_NAME}" || { journalctl -u "${SERVICE_NAME}" -n 120 --no-pager; fail "${SERVICE_NAME} is not active."; }
HTTP_CODE="$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PORT}/" || true)"
[[ "${HTTP_CODE}" == "401" ]] || { journalctl -u "${SERVICE_NAME}" -n 120 --no-pager; fail "Panel HTTP check failed, got code ${HTTP_CODE}."; }

log "Opening UFW ports if UFW is installed..."
if command -v ufw >/dev/null 2>&1; then
  ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true
  ufw allow "${NOVNC_PORT}/tcp" >/dev/null 2>&1 || true
fi

cat <<EOF
============================================================
WLB2 Panel ${VERSION} installed.

URL:          http://${SERVER_IP}:${PORT}
Login:        admin
Password:     ${PASSWORD:-unchanged from previous installation}
noVNC URL:    http://${SERVER_IP}:${NOVNC_PORT}
VNC password: $(cat "${CONF_DIR}/vnc-password.txt")

What changed in WLB2:
  - Permanent client links survive reboot.
  - Every client start call creates a fresh WB Stream room.
  - The same client link can be used by several devices at once.
  - WB cookies and server Chrome profile are preserved.
  - Server-side DNS redirect is built into the creator.

Commands:
  systemctl status ${SERVICE_NAME}
  journalctl -u ${SERVICE_NAME} -f

Open ports at your hosting firewall if needed:
  ${PORT}/tcp for panel and client API
  ${NOVNC_PORT}/tcp for embedded browser/noVNC
============================================================
EOF
