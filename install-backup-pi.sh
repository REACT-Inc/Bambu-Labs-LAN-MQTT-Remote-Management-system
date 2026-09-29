#!/usr/bin/env bash
# Install the backup Discord bot on a SEPARATE Pi (not the one running 3D Printer Management).
# It watches the main Pi over the network and serves /ftcteam while the main bot is offline.
# See backupDiscordBot/BACKUP_BOT.md. Run again from a newer release folder to update it.
#
#   sudo PM_MAIN_URL=http://100.x.y.z:8080 PM_BACKUP_TOKEN=<second bot token> bash install-backup-pi.sh
#
# PM_SYNC_KEY is optional: without it a new key is generated and printed for the main Pi's config.json.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this installer with sudo bash install-backup-pi.sh'; exit 1
fi
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
. "$SOURCE_DIR/platform.sh"
pm_detect_platform
echo "Installing the backup Discord bot on $PM_OS_NAME (package manager: $PM_PKG)."
pm_check_systemd
APP_DIR=/opt/3d-printer-management
DATA_DIR=/var/lib/3d-printer-management
CONFIG_DIR=/etc/3d-printer-management
CONFIG="$CONFIG_DIR/config.json"
if [ -f /etc/systemd/system/3d-printer-management.service ]; then
  echo 'This computer runs the main 3D Printer Management service. The backup bot must run on a different Pi,'
  echo 'so it keeps working when this one is down.'
  exit 1
fi
pm_install_packages
pm_check_python
id printermanager >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell "$(pm_nologin)" printermanager
install -d -m 750 -o printermanager -g printermanager "$DATA_DIR"
install -d -m 750 -o root -g printermanager "$CONFIG_DIR"
install -d -m 755 "$APP_DIR"
# The whole release (the backup only runs the team code, but shares core.py and its helpers).
tar -C "$SOURCE_DIR" --exclude=./tests --exclude=./.git --exclude=./venv --exclude='__pycache__' -cf - . | tar -C "$APP_DIR" -xf -
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --disable-pip-version-check -r "$APP_DIR/requirements.txt"
if [ ! -f "$CONFIG" ]; then
  MAIN_URL="${PM_MAIN_URL:-}"; TOKEN="${PM_BACKUP_TOKEN:-}"
  if [ -t 0 ]; then
    [ -n "$MAIN_URL" ] || read -r -p 'Main dashboard address (for example http://100.101.102.103:8080): ' MAIN_URL
    [ -n "$TOKEN" ] || { read -r -s -p 'Backup Discord bot token (a different bot from the main one): ' TOKEN; echo; }
  fi
  PM_MAIN_URL="$MAIN_URL" PM_BACKUP_TOKEN="$TOKEN" "$APP_DIR/venv/bin/python" - "$CONFIG" <<'PY'
import json,os,secrets,sys
key=os.environ.get('PM_SYNC_KEY') or secrets.token_urlsafe(32)
config={'guild_ids':[],'admin_user_ids':[],'backup_bot':{'enabled':True,'token':os.environ.get('PM_BACKUP_TOKEN',''),
    'main_url':os.environ.get('PM_MAIN_URL','').rstrip('/'),'sync_key':key,'failover_after':90,'failback_after':30}}
with open(sys.argv[1],'w') as f:json.dump(config,f,indent=2)
print('\nAdd this to /etc/3d-printer-management/config.json on the MAIN Pi, then restart it')
print('(sudo systemctl restart 3d-printer-management):\n')
print('  "backup_sync": {"key": "%s"}\n'%key)
PY
fi
chown root:printermanager "$CONFIG"
chmod 640 "$CONFIG"
chown -R printermanager:printermanager "$DATA_DIR"
# Check the settings and that the team code loads before (re)starting the service.
runuser -u printermanager -- env PM_CONFIG="$CONFIG" PM_DATA="$DATA_DIR" "$APP_DIR/venv/bin/python" -c "
import sys; sys.path.insert(0, '$APP_DIR')
import core
from backupDiscordBot.backup_bot import settings_for, problems
import ftcTeamManagement.team_discord
found = problems(settings_for(core.CONFIG), core.DISCORD_BOT_TOKEN)
print('Backup bot settings OK.' if not found else 'Fix in $CONFIG: ' + '; '.join(found))"
bash "$SOURCE_DIR/Updater/install-backup-bot.sh"
echo 'Check it with: sudo journalctl -u pm-backup-bot -n 30 --no-pager'
