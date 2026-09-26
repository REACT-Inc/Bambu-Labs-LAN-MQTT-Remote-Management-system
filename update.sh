#!/usr/bin/env bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run sudo bash update.sh'; exit 1; }
PM_SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PM_APP=/opt/3d-printer-management
[ -x "$PM_APP/venv/bin/python" ] || { echo 'Existing installation missing. Use install.sh for a new installation.'; exit 1; }
[ "$PM_SOURCE" != "$PM_APP" ] || { echo 'Run from the extracted download folder.'; exit 1; }
PM_FILES=(github_updates.py version.py GITHUB_SETUP.md thermal_controls.py web_updates.py update_package.py static/updates.js WEB_UPDATES.md requirements.txt meshcentral_client.py static/team.js README.md LAPTOPS.md plate_swap.py plate_swap_discord.py printer_controls.py controls_discord.py live_camera.py static/index.html static/style.css static/controls.js main.py team.py team_discord.py core.py extra_discord.py printer_files.py printer_errors.py bambu_error_catalog.json BAMBU_RESOURCE_LICENSE.txt ERROR_SOURCES.md camera_capture.py progress_notifications.py queueing.py dashboard.py discord_queue.py static/app.js)
for PM_FILE in "${PM_FILES[@]}"; do
  test -f "$PM_SOURCE/$PM_FILE"

done
"$PM_APP/venv/bin/python" - "$PM_SOURCE" <<'PY'
import ast,sys
from pathlib import Path
for name in ('web_updates.py','update_package.py','update_worker.py','meshcentral_client.py','plate_swap.py','plate_swap_discord.py','printer_controls.py','controls_discord.py','live_camera.py','core.py','main.py','team.py','team_discord.py','extra_discord.py','printer_files.py','printer_errors.py','camera_capture.py','progress_notifications.py','queueing.py','dashboard.py','discord_queue.py'):
    p=Path(sys.argv[1])/name
    ast.parse(p.read_text(),filename=str(p))
PY
PM_BACKUP="$(mktemp -d /var/backups/pm-controls-XXXXXXXX)"
for PM_FILE in "${PM_FILES[@]}"; do
  mkdir -p "$PM_BACKUP/$(dirname "$PM_FILE")"
  if [ -f "$PM_APP/$PM_FILE" ]; then cp -p "$PM_APP/$PM_FILE" "$PM_BACKUP/$PM_FILE"; fi
done
rollback() {
  trap - ERR
  echo "Update failed. Restoring code from $PM_BACKUP"
  for PM_FILE in "${PM_FILES[@]}"; do
    if [ -f "$PM_BACKUP/$PM_FILE" ]; then cp -p "$PM_BACKUP/$PM_FILE" "$PM_APP/$PM_FILE"; else rm -f "$PM_APP/$PM_FILE"; fi
  done
  systemctl restart 3d-printer-management || true
  exit 1
}
trap rollback ERR
systemctl stop 3d-printer-management
for PM_FILE in "${PM_FILES[@]}"; do install -m 644 "$PM_SOURCE/$PM_FILE" "$PM_APP/$PM_FILE"; done
bash "$PM_SOURCE/install-web-updater.sh"
systemctl start 3d-printer-management
"$PM_APP/venv/bin/python" - <<'HEALTH'
import json,time,urllib.request
from pathlib import Path
port=json.loads(Path('/etc/3d-printer-management/config.json').read_text()).get('port',8080)
for attempt in range(30):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=2) as response:
            value=json.load(response)
        if value.get('application')=='3d-printer-management' and 'release' in value:break
    except Exception:pass
    time.sleep(1)
else:raise SystemExit('Updated dashboard failed its startup check.')
HEALTH
trap - ERR
echo "Updated. Code backup: $PM_BACKUP"
echo 'Refresh your dashboard. Discord commands sync when the bot connects.'
echo 'Inspect active queue jobs marked needs_review after the restart before resolving them.'
systemctl status 3d-printer-management --no-pager
