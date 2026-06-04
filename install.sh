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

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запусти от root: sudo bash install.sh"
  exit 1
fi

apt-get update
apt-get install -y curl ca-certificates python3 openssl git golang-go

if ! id -u "$PANEL_USER" >/dev/null 2>&1; then
  useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$PANEL_USER"
fi

mkdir -p "$PANEL_DIR" "$WLB_DIR" "$DATA_DIR/sessions" "$LOG_DIR" "$CONFIG_DIR"

# Install panel.py: prefer local file, otherwise download from GitHub raw.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/panel.py" ]]; then
  cp "$SCRIPT_DIR/panel.py" "$PANEL_DIR/panel.py"
else
  echo "Скачиваю panel.py..."
  curl -fL "$PANEL_URL" -o "$PANEL_DIR/panel.py"
fi
chmod +x "$PANEL_DIR/panel.py"

# Sanity check: panel.py must be valid Python with real newlines.
python3 -m py_compile "$PANEL_DIR/panel.py"

# Try downloading latest release binary using GitHub API.
echo "Скачиваю headless-wbstream-creator..."
TMP_JSON="$(mktemp)"
URL=""
if curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" -o "$TMP_JSON"; then
  URL="$(python3 - <<'PY' "$TMP_JSON"
import json, re, sys
p=sys.argv[1]
data=json.load(open(p))
assets=data.get('assets', [])
patterns=[
    r'headless-wbstream-creator.*linux.*(x64|amd64)',
    r'headless-wbstream.*creator.*linux',
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

if [[ -n "${URL:-}" ]]; then
  curl -fL "$URL" -o "$WLB_DIR/headless-wbstream-creator" || URL=""
fi

# Fallback: try known names.
if [[ ! -s "$WLB_DIR/headless-wbstream-creator" ]]; then
  for name in \
    headless-wbstream-creator-linux-x64 \
    headless-wbstream-creator-linux-amd64 \
    headless-wbstream-creator-linux-x86_64; do
    if curl -fL "https://github.com/${REPO}/releases/latest/download/${name}" -o "$WLB_DIR/headless-wbstream-creator"; then
      break
    fi
  done
fi

# Fallback: build from source.
if [[ ! -s "$WLB_DIR/headless-wbstream-creator" ]]; then
  echo "Готовый бинарник не найден, собираю из исходников..."
  BUILD_DIR="/tmp/whitelist-bypass-build-$$"
  git clone --depth 1 "https://github.com/${REPO}.git" "$BUILD_DIR"
  cd "$BUILD_DIR"
  if [[ -x ./build-headless.sh ]]; then
    ./build-headless.sh || true
  fi
  FOUND="$(find "$BUILD_DIR" -type f -name '*wbstream*creator*' -perm -111 | head -n 1 || true)"
  if [[ -z "$FOUND" ]]; then
    if [[ -d "$BUILD_DIR/headless/wbstream" ]]; then
      (cd "$BUILD_DIR/headless/wbstream" && go build -trimpath -ldflags="-s -w" -o "$WLB_DIR/headless-wbstream-creator" .)
    fi
  else
    cp "$FOUND" "$WLB_DIR/headless-wbstream-creator"
  fi
  rm -rf "$BUILD_DIR"
fi

if [[ ! -s "$WLB_DIR/headless-wbstream-creator" ]]; then
  echo "Не удалось установить headless-wbstream-creator. Проверь Releases проекта ${REPO}."
  exit 1
fi
chmod +x "$WLB_DIR/headless-wbstream-creator"

PASSWORD="$(openssl rand -base64 18 | tr -d '=+/ ' | cut -c1-16)"
PASS_HASH="$(python3 - <<'PY' "$PASSWORD"
import hashlib, sys
print(hashlib.sha256(sys.argv[1].encode()).hexdigest())
PY
)"

cat > "$CONFIG_DIR/config.json" <<JSON
{
  "username": "admin",
  "password_sha256": "$PASS_HASH"
}
JSON

cat > /etc/systemd/system/wlb-panel.service <<EOF
[Unit]
Description=WLB Panel for whitelist-bypass WB Stream
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=WLB_PORT=$PORT
Environment=WLB_HOST=0.0.0.0
Environment=WLB_CONFIG=$CONFIG_DIR/config.json
Environment=WLB_STATE=$DATA_DIR/state.json
Environment=WLB_SESSIONS=$DATA_DIR/sessions
Environment=WLB_LOGS=$LOG_DIR
Environment=WLB_CREATOR=$WLB_DIR/headless-wbstream-creator
Environment=WLB_RESOURCES=moderate
ExecStart=/usr/bin/python3 $PANEL_DIR/panel.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

chown -R root:root "$PANEL_DIR" "$WLB_DIR" "$CONFIG_DIR"
chmod 600 "$CONFIG_DIR/config.json"

systemctl daemon-reload
systemctl enable --now wlb-panel

IP="$(curl -4fsSL https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"

echo
printf '%s\n' "=============================================="
printf '%s\n' "WLB Panel installed"
printf '%s\n' "URL:      http://${IP}:${PORT}"
printf '%s\n' "Login:    admin"
printf '%s\n' "Password: ${PASSWORD}"
printf '%s\n' "=============================================="
echo
printf '%s\n' "Команды:"
printf '%s\n' "  systemctl status wlb-panel"
printf '%s\n' "  journalctl -u wlb-panel -f"
printf '%s\n' "  systemctl restart wlb-panel"
