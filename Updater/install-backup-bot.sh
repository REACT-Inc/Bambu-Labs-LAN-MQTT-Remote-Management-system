#!/usr/bin/env bash
# Install (or refresh) the backup Discord bot service. Safe to run repeatedly. It idles until
# config.json has "backup_bot": {"enabled": true, "token": "<second bot token>"}.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo.'; exit 1; }
cat > /etc/systemd/system/pm-backup-bot.service <<'UNIT'
[Unit]
Description=3D Printer Management - backup Discord bot (team commands while the main bot is offline)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=printermanager
Group=printermanager
WorkingDirectory=/opt/3d-printer-management
Environment=PYTHONUNBUFFERED=1
Environment=PM_CONFIG=/etc/3d-printer-management/config.json
Environment=PM_DATA=/var/lib/3d-printer-management
ExecStart=/opt/3d-printer-management/venv/bin/python /opt/3d-printer-management/backupDiscordBot/backup_bot.py
# Also restarts after a new release is installed (the bot exits so the new code loads).
Restart=always
RestartSec=10
UMask=0077
NoNewPrivileges=true
MemoryMax=200M

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable pm-backup-bot >/dev/null 2>&1 || true
systemctl restart pm-backup-bot || true
echo 'Backup Discord bot service installed (pm-backup-bot).'
