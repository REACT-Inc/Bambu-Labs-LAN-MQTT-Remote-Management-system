#!/usr/bin/env bash
# The dashboard at printers.<your domain>, behind a Google sign-in (Cloudflare Tunnel + Cloudflare Access).
# Installs cloudflared, connects the tunnel you made in Cloudflare Zero Trust, and saves the Access settings the
# dashboard checks each request against. Steps and the Cloudflare side: docs/cloudflare-access.md.
#
#   sudo bash cloudflare-tunnel.sh --token <tunnel token> --hostname printers.example.org \
#        --team yourteam.cloudflareaccess.com --aud <Application Audience tag> --admin you@example.org [--admin ...] \
#        [--lan printing|full|off]
#   sudo bash cloudflare-tunnel.sh --remove      # stop the tunnel and turn the Google sign-in off again
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this with sudo bash cloudflare-tunnel.sh ...'; exit 1
fi
CONFIG=/etc/3d-printer-management/config.json
PYTHON=/opt/3d-printer-management/venv/bin/python
TOKEN='' HOSTNAME_='' TEAM='' AUD='' LAN=printing REMOVE=0
ADMINS=()
usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="${2:-}"; shift 2 ;;
    --hostname) HOSTNAME_="${2:-}"; shift 2 ;;
    --team) TEAM="${2:-}"; shift 2 ;;
    --aud) AUD="${2:-}"; shift 2 ;;
    --admin) ADMINS+=("${2:-}"); shift 2 ;;
    --lan) LAN="${2:-}"; shift 2 ;;
    --remove) REMOVE=1; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done
[ -f "$CONFIG" ] || { echo "No $CONFIG: install the dashboard first (sudo bash install.sh)."; exit 1; }
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

if [ "$REMOVE" = 1 ]; then
  if command -v cloudflared >/dev/null; then cloudflared service uninstall || true; fi
  "$PYTHON" - "$CONFIG" <<'EOF'
import json, os, sys, tempfile
path = sys.argv[1]
config = json.load(open(path))
config.pop('cloudflare_access', None)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, 'w') as f: json.dump(config, f, indent=2)
os.chmod(tmp, os.stat(path).st_mode); os.chown(tmp, os.stat(path).st_uid, os.stat(path).st_gid); os.replace(tmp, path)
EOF
  systemctl restart 3d-printer-management || true
  echo 'The tunnel is stopped and the Google sign-in is off. (Delete the tunnel and the Access application in Cloudflare too.)'
  exit 0
fi

[ -n "$TOKEN" ] && [ -n "$HOSTNAME_" ] && [ -n "$TEAM" ] && [ -n "$AUD" ] || { echo 'Needs --token, --hostname, --team and --aud.'; usage; }
case "$LAN" in printing|full|off) ;; *) echo '--lan is printing, full or off.'; exit 1 ;; esac
case "$HOSTNAME_" in *.*) ;; *) echo "--hostname is the full address, for example printers.example.org"; exit 1 ;; esac
TEAM="${TEAM#https://}"; TEAM="${TEAM%/}"
case "$TEAM" in *.cloudflareaccess.com) ;; *) TEAM="$TEAM.cloudflareaccess.com" ;; esac
[ ${#ADMINS[@]} -gt 0 ] || echo 'No --admin given: every Google account your Access policy lets in can use management.'

if ! command -v cloudflared >/dev/null; then
  echo 'Installing cloudflared...'
  if command -v apt-get >/dev/null; then
    # Cloudflare's own package repository (Raspberry Pi OS, Debian, Ubuntu)
    install -d -m 0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
    echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' > /etc/apt/sources.list.d/cloudflared.list
    apt-get update -q && apt-get install -y -q cloudflared
  else
    case "$(uname -m)" in
      x86_64|amd64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; armv7l|armv6l|arm*) ARCH=arm ;;
      *) echo "No cloudflared download for $(uname -m): install it yourself, then run this again."; exit 1 ;;
    esac
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH" -o /usr/local/bin/cloudflared
    chmod 755 /usr/local/bin/cloudflared
  fi
fi
cloudflared --version

# The tunnel runs as its own service, with the token from the Cloudflare dashboard (Networks -> Tunnels).
if systemctl list-unit-files cloudflared.service >/dev/null 2>&1 && systemctl cat cloudflared >/dev/null 2>&1; then
  echo 'Replacing the existing cloudflared service with this tunnel...'
  cloudflared service uninstall || true
fi
cloudflared service install "$TOKEN"
systemctl enable --now cloudflared

ADMINS_JSON="$(printf '%s\n' "${ADMINS[@]+"${ADMINS[@]}"}" | "$PYTHON" -c 'import json,sys;print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"
"$PYTHON" - "$CONFIG" "$TEAM" "$AUD" "$HOSTNAME_" "$ADMINS_JSON" "$LAN" <<'EOF'
import json, os, sys, tempfile
path, team, aud, hostname, admins, lan = sys.argv[1:]
config = json.load(open(path))
config['cloudflare_access'] = {'team_domain': team, 'aud': aud, 'hostname': hostname.lower(), 'admins': json.loads(admins), 'lan': lan}
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, 'w') as f: json.dump(config, f, indent=2)
os.chmod(tmp, os.stat(path).st_mode); os.chown(tmp, os.stat(path).st_uid, os.stat(path).st_gid); os.replace(tmp, path)
EOF
systemctl restart 3d-printer-management
PORT="$("$PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("port",8080))' "$CONFIG")"
echo
echo "Done. Open https://$HOSTNAME_ and sign in with Google."
echo "In Cloudflare, the tunnel's public hostname $HOSTNAME_ must point at http://localhost:$PORT."
echo "The Pi's own addresses now allow: $LAN (change with --lan, see docs/cloudflare-access.md)."
