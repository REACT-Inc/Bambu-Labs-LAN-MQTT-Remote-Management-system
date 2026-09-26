#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this installer with sudo bash install.sh'; exit 1
fi
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/3d-printer-management
DATA_DIR=/var/lib/3d-printer-management
CONFIG_DIR=/etc/3d-printer-management
BACKUP_DIR="/var/backups/3d-printer-management/$(date +%Y%m%d-%H%M%S)"
OLD_WAS_ACTIVE=0
NEW_WAS_ACTIVE=0
systemctl is-active --quiet printer-discord-bot && OLD_WAS_ACTIVE=1
systemctl is-active --quiet 3d-printer-management && NEW_WAS_ACTIVE=1
LISTEN_IP="${PM_LISTEN_IP:-}"
if [ -z "$LISTEN_IP" ] && command -v tailscale >/dev/null; then
  LISTEN_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
fi
if [ -z "$LISTEN_IP" ]; then
  echo 'Tailscale is not connected. Connect it first, or explicitly set PM_LISTEN_IP to the Pi LAN IP.'
  echo 'Example: sudo PM_LISTEN_IP=192.168.1.50 bash install.sh'
  exit 1
fi
apt-get update
apt-get install -y python3 python3-venv ffmpeg sudo
id printermanager >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin printermanager
install -d -m 750 -o printermanager -g printermanager "$DATA_DIR"
install -d -m 750 -o root -g printermanager "$CONFIG_DIR"
install -d -m 700 "$BACKUP_DIR"
if [ -f /tmp/printer_discord_bot.py ]; then cp -p /tmp/printer_discord_bot.py "$BACKUP_DIR/legacy_bot.py"; fi
if [ -d "$APP_DIR" ]; then
  install -d "$BACKUP_DIR/app"
  cp -a "$APP_DIR/." "$BACKUP_DIR/app/"
fi
if [ -f /etc/systemd/system/3d-printer-management.service ]; then cp -p /etc/systemd/system/3d-printer-management.service "$BACKUP_DIR/management.service"; fi
if [ -f "$CONFIG_DIR/config.json" ]; then cp -p "$CONFIG_DIR/config.json" "$BACKUP_DIR/config.json"; fi
install -d -m 755 "$APP_DIR"
for file in github_updates.py version.py thermal_controls.py web_updates.py update_package.py WEB_UPDATES.md meshcentral_client.py plate_swap.py plate_swap_discord.py printer_controls.py controls_discord.py live_camera.py core.py extra_discord.py printer_files.py printer_errors.py bambu_error_catalog.json BAMBU_RESOURCE_LICENSE.txt ERROR_SOURCES.md camera_capture.py progress_notifications.py queueing.py dashboard.py discord_queue.py main.py configure.py team.py team_discord.py requirements.txt README.md LAPTOPS.md; do
  install -m 644 "$SOURCE_DIR/$file" "$APP_DIR/$file"
done
install -d "$APP_DIR/static"
cp -a "$SOURCE_DIR/static/." "$APP_DIR/static/"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --disable-pip-version-check -r "$APP_DIR/requirements.txt"
"$APP_DIR/venv/bin/python" "$APP_DIR/configure.py" --listen "$LISTEN_IP" "$@"
chown root:printermanager "$CONFIG_DIR/config.json"
chmod 640 "$CONFIG_DIR/config.json"
chown -R printermanager:printermanager "$DATA_DIR"
find "$DATA_DIR" -type f -exec chmod 600 {} +
# Validate command definitions and configuration before stopping the old bot.
runuser -u printermanager -- "$APP_DIR/venv/bin/python" -c "import sys; sys.path.insert(0, '$APP_DIR'); import core; from discord_queue import install; from queueing import Store, Engine; from dashboard import Dashboard; s=Store(':memory:'); e=Engine(core,s); d=Dashboard(core,s,e); install(core,s,e,d); from team import Team; from team_discord import install as it; t=Team(core,s,d); it(core,t); print('Application validation passed.')"
cat > /etc/systemd/system/3d-printer-management.service <<'UNIT'
[Unit]
Description=3D Printer Management - web dashboard, Discord and shared print queues
Wants=network-online.target tailscaled.service
After=network-online.target tailscaled.service

[Service]
Type=simple
User=printermanager
Group=printermanager
WorkingDirectory=/opt/3d-printer-management
Environment=PYTHONUNBUFFERED=1
Environment=PM_CONFIG=/etc/3d-printer-management/config.json
Environment=PM_DATA=/var/lib/3d-printer-management
ExecStart=/opt/3d-printer-management/venv/bin/python /opt/3d-printer-management/main.py
Restart=on-failure
RestartSec=10
TimeoutStopSec=45
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/3d-printer-management
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
if "$APP_DIR/venv/bin/python" -c "import json; c=json.load(open('/etc/3d-printer-management/config.json')); raise SystemExit(0 if c.get('allow_host_reboot') else 1)"; then
  cat > /usr/local/sbin/pm-host-reboot <<'REBOOT'
#!/bin/sh
[ "$#" -eq 0 ] || exit 64
exec /usr/bin/systemctl reboot
REBOOT
  chmod 755 /usr/local/sbin/pm-host-reboot
  chown root:root /usr/local/sbin/pm-host-reboot
  printf 'printermanager ALL=(root) NOPASSWD: /usr/local/sbin/pm-host-reboot ""\n' > /etc/sudoers.d/printermanager-reboot
  chmod 440 /etc/sudoers.d/printermanager-reboot
  visudo -cf /etc/sudoers.d/printermanager-reboot
  sed -i 's/NoNewPrivileges=true/NoNewPrivileges=false/' /etc/systemd/system/3d-printer-management.service
fi
systemctl daemon-reload
systemctl stop printer-discord-bot 2>/dev/null || true
systemctl restart 3d-printer-management || true
HEALTHY=0
for attempt in $(seq 1 15); do
  if systemctl is-active --quiet 3d-printer-management && "$APP_DIR/venv/bin/python" -c "import json,urllib.request; c=json.load(open('/etc/3d-printer-management/config.json')); d=json.load(urllib.request.urlopen('http://127.0.0.1:'+str(c.get('port',8080))+'/health',timeout=2)); assert d.get('application')=='3d-printer-management'" 2>/dev/null; then HEALTHY=1; break; fi
  sleep 1
done
if [ "$HEALTHY" -ne 1 ]; then
  echo 'Startup failed. Rolling back the previous service where available.'
  systemctl stop 3d-printer-management || true
  if [ -d "$BACKUP_DIR/app" ]; then cp -a "$BACKUP_DIR/app/." "$APP_DIR/"; fi
  if [ -f "$BACKUP_DIR/config.json" ]; then cp -p "$BACKUP_DIR/config.json" "$CONFIG_DIR/config.json"; fi
  if [ -f "$BACKUP_DIR/management.service" ]; then cp -p "$BACKUP_DIR/management.service" /etc/systemd/system/3d-printer-management.service; fi
  systemctl daemon-reload
  if [ "$OLD_WAS_ACTIVE" -eq 1 ]; then systemctl start printer-discord-bot; fi
  if [ "$NEW_WAS_ACTIVE" -eq 1 ]; then systemctl start 3d-printer-management; fi
  journalctl -u 3d-printer-management -n 30 --no-pager
  exit 1
fi
bash "$SOURCE_DIR/install-web-updater.sh"
systemctl enable 3d-printer-management
systemctl disable printer-discord-bot 2>/dev/null || true
# Print actual configured addresses, including on subsequent installs.
"$APP_DIR/venv/bin/python" - <<'PY'
import json
from pathlib import Path
c=json.loads(Path('/etc/3d-printer-management/config.json').read_text())
print('\n3D Printer Management is running.')
for host in c['listen']:
    print(f"Open: http://{host}:{c['port']}")
print('Use the initial password shown above, or your existing dashboard password.')
PY
printf 'Backup: %s\n' "$BACKUP_DIR"
