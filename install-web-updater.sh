#!/usr/bin/env bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || exit 1
PM_SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
install -d -o root -g root -m 755 /usr/local/lib/pm-updater /var/lib/pm-updater
install -d -o root -g root -m 700 /var/lib/pm-updater/backups
install -d -o printermanager -g printermanager -m 700 /var/lib/3d-printer-management/updates
install -o root -g root -m 644 "$PM_SRC/update_worker.py" /usr/local/lib/pm-updater/worker.py
install -o root -g root -m 644 "$PM_SRC/update_package.py" /usr/local/lib/pm-updater/update_package.py
cat > /etc/systemd/system/pm-web-update.service <<'UNIT'
[Unit]
Description=Install a reviewed 3D Printer Management release
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I /usr/local/lib/pm-updater/worker.py
TimeoutStartSec=30min
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/pm-web-update.path <<'UNIT'
[Unit]
Description=Watch for a reviewed management ZIP
[Path]
PathExists=/var/lib/3d-printer-management/updates/request.json
Unit=pm-web-update.service
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable pm-web-update.service
systemctl enable --now pm-web-update.path
