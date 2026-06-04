#!/usr/bin/env bash
set -Eeuo pipefail

VERSION="alpha 0.0.2"
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
WB_RELEASE_TAG="v0.3.5"

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
# The panel has a dedicated system user. Stopping only that user's processes
# cannot accidentally kill this root installer when it is run through bash -c.
if id -u "${USER_NAME}" >/dev/null 2>&1; then
  pkill -TERM -u "${USER_NAME}" 2>/dev/null || true
  sleep 1
  pkill -KILL -u "${USER_NAME}" 2>/dev/null || true
fi

log "Updating apt indexes..."
apt-get update -y

log "Installing base dependencies..."
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

# Ubuntu 24.04 uses t64 package names, while older supported Ubuntu releases do not.
apt-get install -y --no-install-recommends libatk-bridge2.0-0t64 libgtk-3-0t64 libasound2t64 2>/dev/null \
  || apt-get install -y --no-install-recommends libatk-bridge2.0-0 libgtk-3-0 libasound2

command -v go >/dev/null || fail "Go was not installed. Check apt repository availability."
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
  apt-get install -y google-chrome-stable || warn "google-chrome-stable install failed; will try chromium fallback."
fi

if ! command -v google-chrome >/dev/null && ! command -v google-chrome-stable >/dev/null; then
  warn "Google Chrome not found; trying chromium-browser fallback."
  apt-get install -y chromium-browser || apt-get install -y chromium || true
fi

if ! command -v google-chrome >/dev/null && ! command -v google-chrome-stable >/dev/null && ! command -v chromium-browser >/dev/null && ! command -v chromium >/dev/null; then
  fail "No Chrome/Chromium executable found after installation."
fi

log "Creating system user and directories..."
id -u "${USER_NAME}" >/dev/null 2>&1 || useradd --system --home "${STATE_DIR}" --shell /usr/sbin/nologin "${USER_NAME}"
mkdir -p "${PANEL_DIR}" "${WB_DIR}" "${STATE_DIR}/sessions" "${STATE_DIR}/chrome-profile" "${STATE_DIR}/runtime" "${LOG_DIR}" "${CONF_DIR}"
# Old WB Stream rooms cannot be safely resumed after their creator stops.
# Start with a clean session list so the panel never shows stale join links.
rm -rf "${STATE_DIR}/sessions"
mkdir -p "${STATE_DIR}/sessions"
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

# Current upstream releases package all headless Linux tools in one CLI ZIP.
case "$(dpkg --print-architecture)" in
  amd64) CLI_ARCH="x64" ;;
  arm64) CLI_ARCH="arm64" ;;
  i386) CLI_ARCH="ia32" ;;
  *) CLI_ARCH="" ;;
esac
if [[ -n "${CLI_ARCH}" ]]; then
  RELEASE_JSON="$(curl -fsSL --connect-timeout 10 --max-time 60 "https://api.github.com/repos/kulikov0/whitelist-bypass/releases/tags/${WB_RELEASE_TAG}" || true)"
  CLI_URL="$(jq -r --arg name "whitelist-bypass-cli-linux-${CLI_ARCH}.zip" '.assets[]? | select(.name == $name) | .browser_download_url' <<<"${RELEASE_JSON}" | head -n 1)"
  if [[ -n "${CLI_URL}" ]]; then
    log "Downloading creator from upstream release ${WB_RELEASE_TAG}..."
    if curl -fL --retry 3 --connect-timeout 10 --max-time 180 "${CLI_URL}" -o /tmp/wlb-cli.zip; then
      unzip -p /tmp/wlb-cli.zip headless-wbstream-creator > "${CREATOR_TARGET}.tmp" || true
      if [[ -s "${CREATOR_TARGET}.tmp" ]]; then
        mv "${CREATOR_TARGET}.tmp" "${CREATOR_TARGET}"
      fi
    fi
  fi
fi
rm -f /tmp/wlb-cli.zip "${CREATOR_TARGET}.tmp"

if [[ ! -s "${CREATOR_TARGET}" ]]; then
  log "Prebuilt creator not found. Building whitelist-bypass from source..."
  rm -rf /tmp/whitelist-bypass-src
  git clone --depth 1 --branch "${WB_RELEASE_TAG}" "${WB_REPO}" /tmp/whitelist-bypass-src
  go -C /tmp/whitelist-bypass-src/headless/wbstream build -trimpath -ldflags="-s -w" -o headless-wbstream-creator .
  FOUND_CREATOR="/tmp/whitelist-bypass-src/headless/wbstream/headless-wbstream-creator"
  [[ -s "${FOUND_CREATOR}" ]] || fail "Build finished, but headless-wbstream-creator was not found."
  cp "${FOUND_CREATOR}" "${CREATOR_TARGET}"
fi

# The Android app's System DNS may point to the phone's local router
# (for example 192.168.1.1). That address is not reachable from the server.
# Build the creator with a server-side port-53 redirect so the phone works
# without changing DNS settings in the Android app.
log "Building creator with automatic server-side DNS redirect..."
DNS_BUILD_DIR="/tmp/whitelist-bypass-dns-src"
rm -rf "${DNS_BUILD_DIR}"
git clone --depth 1 --branch "${WB_RELEASE_TAG}" "${WB_REPO}" "${DNS_BUILD_DIR}"
python3 - "${DNS_BUILD_DIR}/relay/tunnel/relay_bridge.go" <<'PY'
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
go -C "${DNS_BUILD_DIR}/headless/wbstream" build -trimpath -ldflags="-s -w" -o "${CREATOR_TARGET}" .
rm -rf "${DNS_BUILD_DIR}"

chmod 755 "${CREATOR_TARGET}"
"${CREATOR_TARGET}" --help >/dev/null 2>&1 || warn "Creator exists but --help returned non-zero; continuing."
ls -lh "${CREATOR_TARGET}"

log "Creating initial config..."
PASSWORD=""
if [[ ! -s "${CONF_DIR}/config.json" ]] || ! jq -e '.username | type == "string" and length > 0' "${CONF_DIR}/config.json" >/dev/null 2>&1 \
  || ! jq -e '.password_sha256 | type == "string" and length == 64' "${CONF_DIR}/config.json" >/dev/null 2>&1; then
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
else
  log "Keeping existing panel login and password."
fi

if [[ ! -s "${CONF_DIR}/vnc.pass" ]] || [[ ! -s "${CONF_DIR}/vnc-password.txt" ]]; then
  VNC_PASSWORD="$(openssl rand -base64 18 | tr -d '=+/ ' | cut -c1-8)"
  x11vnc -storepasswd "${VNC_PASSWORD}" "${CONF_DIR}/vnc.pass" >/dev/null
  printf '%s\n' "${VNC_PASSWORD}" > "${CONF_DIR}/vnc-password.txt"
fi
chown -R "${USER_NAME}:${USER_NAME}" "${CONF_DIR}" "${STATE_DIR}" "${LOG_DIR}"
chmod 600 "${CONF_DIR}/config.json"
chmod 600 "${CONF_DIR}/vnc.pass" "${CONF_DIR}/vnc-password.txt"

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
systemctl enable wlb-panel >/dev/null
systemctl restart wlb-panel
sleep 2
systemctl is-active --quiet wlb-panel || { journalctl -u wlb-panel -n 80 --no-pager; fail "wlb-panel service is not active."; }
curl --noproxy '*' -fsS --max-time 5 "http://127.0.0.1:${PORT}/login" >/dev/null || { journalctl -u wlb-panel -n 80 --no-pager; fail "Panel service is active but HTTP check failed."; }

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
Password: ${PASSWORD:-unchanged from previous installation}
VNC password: $(cat "${CONF_DIR}/vnc-password.txt")

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
