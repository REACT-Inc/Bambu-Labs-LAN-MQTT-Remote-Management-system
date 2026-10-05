#!/usr/bin/env bash
# Installs or updates pm-doctor (#43), the independent watchdog. Run as root by install.sh and Updater/update.sh.
# It lives outside the app (/usr/local/lib/pm-doctor) so app updates can't break it, and is replaced only when its
# own DOCTOR_VERSION changes. PM_DOCTOR=0 skips it. Never fails the app install.
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run as root.'; exit 0; }
[ "${PM_DOCTOR:-1}" = "0" ] && { echo 'pm-doctor: skipped (PM_DOCTOR=0).'; exit 0; }
command -v systemctl >/dev/null || { echo 'pm-doctor: needs systemd; skipped.'; exit 0; }
SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../doctor" && pwd)"
LIB=/usr/local/lib/pm-doctor
new="$(/usr/bin/python3 -I "$SRC/pm_doctor.py" --version 2>/dev/null)" || { echo 'pm-doctor: the new copy does not run; keeping the installed one.'; exit 0; }
old="$(/usr/bin/python3 -I "$LIB/pm_doctor.py" --version 2>/dev/null || true)"
install -d -o root -g root -m 755 "$LIB" /etc/pm-doctor
install -d -o root -g root -m 700 /var/lib/pm-doctor
if [ ! -f /etc/pm-doctor/config.json ]; then
  printf '%s\n' '{"discord_webhook": "", "github": {"repository": "", "token": ""}, "restart_after_minutes": 0, "status_page": true, "port": 8081, "interval": 30}' > /etc/pm-doctor/config.json
  chmod 600 /etc/pm-doctor/config.json
fi
if [ "$new" = "$old" ] && [ -f /etc/systemd/system/pm-doctor.service ]; then
  echo "pm-doctor $old is up to date."
  systemctl is-active --quiet pm-doctor || systemctl restart pm-doctor || true
  exit 0
fi
install -o root -g root -m 755 "$SRC/pm_doctor.py" "$LIB/pm_doctor.py"
ln -sf "$LIB/pm_doctor.py" /usr/local/sbin/pm-doctor
cat > /etc/systemd/system/pm-doctor.service <<'UNIT'
[Unit]
Description=pm-doctor: independent watchdog for 3D Printer Management
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/bin/python3 -I /usr/local/lib/pm-doctor/pm_doctor.py run
Restart=always
RestartSec=10
# Never slow the printers' Pi down.
CPUQuota=5%
MemoryMax=64M
Nice=10
# Root only to read the journal, send the frozen app SIGUSR1 and (when enabled) restart it; everything else is locked down.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/pm-doctor
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable pm-doctor >/dev/null 2>&1 || true
systemctl restart pm-doctor || true
echo "pm-doctor ${new} installed${old:+ (was $old)}. Status page on port 8081 (dashboard password); 'sudo pm-doctor' for a check."
exit 0
