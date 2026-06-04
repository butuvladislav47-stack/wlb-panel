#!/usr/bin/env bash
set -Eeuo pipefail

[[ ${EUID} -eq 0 ]] || { echo 'Run as root' >&2; exit 1; }
command -v patch >/dev/null || { apt-get update -y && apt-get install -y patch; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL https://raw.githubusercontent.com/butuvladislav47-stack/wlb-panel/main/panel-hotfix.patch -o "$tmp/panel.patch"

systemctl stop wlb-panel 2>/dev/null || true
cp /opt/wlb-panel/panel.py "/opt/wlb-panel/panel.py.backup.$(date +%s)"
patch --batch --forward /opt/wlb-panel/panel.py < "$tmp/panel.patch"
python3 -m py_compile /opt/wlb-panel/panel.py

if ! grep -q '^KillMode=process$' /etc/systemd/system/wlb-panel.service; then
  sed -i '/^RestartSec=3$/a KillMode=process' /etc/systemd/system/wlb-panel.service
fi

chown wlbpanel:wlbpanel /opt/wlb-panel/panel.py
chmod 755 /opt/wlb-panel/panel.py
systemctl daemon-reload
systemctl restart wlb-panel
sleep 7
systemctl is-active --quiet wlb-panel
echo 'WLB Panel repair applied. Existing links will be restored automatically.'
