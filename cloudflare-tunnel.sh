#!/usr/bin/env bash
# The dashboard at printers.<your domain>, behind a Google sign-in (Cloudflare Tunnel + Cloudflare Access).
# Installs cloudflared, connects the tunnel you made in Cloudflare Zero Trust, saves the Access settings the dashboard
# checks each request against, then checks the address really asks for a sign-in. Steps: docs/cloudflare-access.md.
#
#   sudo bash cloudflare-tunnel.sh                       # asks for each value
#   sudo bash cloudflare-tunnel.sh --token <tunnel token> --domain example.org --team yourteam \
#        --aud <Application Audience tag> --admin you@example.org [--admin @example.org] [--lan printing|full|off]
#   (--hostname printers.example.org instead of --domain for another name; --tunnel-only to keep the settings)
#   sudo bash cloudflare-tunnel.sh --remove              # stop the tunnel and turn the Google sign-in off
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this with sudo bash cloudflare-tunnel.sh ...'; exit 1
fi
CONFIG=/etc/3d-printer-management/config.json
DATA=/var/lib/3d-printer-management
SETTINGS="$DATA/cloudflare_access.json"
PYTHON=/opt/3d-printer-management/venv/bin/python
TOKEN='' HOST='' TEAM='' AUD='' LAN='' REMOVE=0 TUNNEL_ONLY=0
ADMINS=()
usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="${2:-}"; shift 2 ;;
    --domain) HOST="printers.${2:-}"; shift 2 ;;
    --hostname) HOST="${2:-}"; shift 2 ;;
    --team) TEAM="${2:-}"; shift 2 ;;
    --aud) AUD="${2:-}"; shift 2 ;;
    --admin) ADMINS+=("${2:-}"); shift 2 ;;
    --lan) LAN="${2:-}"; shift 2 ;;
    --tunnel-only) TUNNEL_ONLY=1; shift ;;
    --remove) REMOVE=1; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done
[ -f "$CONFIG" ] || { echo "No $CONFIG: install the dashboard first (sudo bash install.sh)."; exit 1; }
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
OWNER="$(stat -c %U "$DATA" 2>/dev/null || echo root)"

save_settings() {   # $1: JSON. Owned by the dashboard's user, which may change it from Settings & help.
  printf '%s\n' "$1" > "$SETTINGS.tmp"
  chmod 600 "$SETTINGS.tmp"; chown "$OWNER" "$SETTINGS.tmp"; mv -f "$SETTINGS.tmp" "$SETTINGS"
}

if [ "$REMOVE" = 1 ]; then
  if command -v cloudflared >/dev/null; then cloudflared service uninstall || true; fi
  save_settings '{}'   # (empty: "cloudflare_access" in config.json isn't used either)
  systemctl restart 3d-printer-management || true
  echo 'The tunnel is stopped and the Google sign-in is off. (Delete the tunnel and the Access application in Cloudflare too.)'
  exit 0
fi

# Anything not given on the command line is asked for (when someone is at the keyboard).
ask() {   # ask VAR "question" [default]
  local value=''
  if [ ! -t 0 ] && [ ! -r /dev/tty ]; then echo "Missing: $2 (pass it as an option; see --help)"; exit 1; fi
  read -r -p "$2${3:+ [$3]}: " value </dev/tty || true
  printf -v "$1" '%s' "${value:-${3:-}}"
}
[ -n "$TOKEN" ] || ask TOKEN 'Tunnel token (Zero Trust -> Networks -> Tunnels -> your tunnel; the long code after "service install")'
if [ "$TUNNEL_ONLY" = 0 ]; then
  if [ -z "$HOST" ]; then ask DOMAIN 'Your domain (the dashboard will be at printers.<domain>)'; HOST="printers.${DOMAIN:-}"; fi
  [ -n "$TEAM" ] || ask TEAM 'Zero Trust team name or domain (yourteam or yourteam.cloudflareaccess.com)'
  [ -n "$AUD" ] || ask AUD "Access application's Application Audience (AUD) tag"
  if [ ${#ADMINS[@]} -eq 0 ]; then
    ask ADMIN_LIST 'Admin Google accounts, separated by spaces (@example.org for a whole domain; empty: everyone Access lets in)'
    read -r -a ADMINS <<< "${ADMIN_LIST:-}"
  fi
  [ -n "$LAN" ] || ask LAN "What the Pi's own addresses (IP, .local, Tailscale) may use: printing, full or off" printing
fi
[ -n "$TOKEN" ] || { echo 'A tunnel token is needed.'; exit 1; }

# Check and save the settings first (the same checks as Settings & help -> Google sign-in), so a typo stops here.
if [ "$TUNNEL_ONLY" = 0 ]; then
  ADMINS_TEXT="$(printf '%s\n' "${ADMINS[@]+"${ADMINS[@]}"}")"
  SETTINGS_JSON="$(cd /opt/3d-printer-management 2>/dev/null || cd "$(dirname "$0")"; "$PYTHON" -c '
import json, sys
sys.path.insert(0, ".")
from cloudflare_access import clean
try:
    print(json.dumps(clean({"hostname": sys.argv[1], "team_domain": sys.argv[2], "aud": sys.argv[3], "admins": sys.argv[4], "lan": sys.argv[5]})))
except ValueError as exc:
    sys.exit(str(exc))' "$HOST" "$TEAM" "$AUD" "$ADMINS_TEXT" "$LAN")" || exit 1
  HOST="$("$PYTHON" -c 'import json,sys;print(json.loads(sys.argv[1])["hostname"])' "$SETTINGS_JSON")"
  TEAM="$("$PYTHON" -c 'import json,sys;print(json.loads(sys.argv[1])["team_domain"])' "$SETTINGS_JSON")"
  [ ${#ADMINS[@]} -gt 0 ] || echo 'No admins given: every Google account your Access policy lets in can use management.'
fi

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

# The tunnel runs as its own service, with the token from the Cloudflare dashboard.
if systemctl cat cloudflared >/dev/null 2>&1; then
  echo 'Replacing the existing cloudflared service with this tunnel...'
  cloudflared service uninstall || true
fi
cloudflared service install "$TOKEN"
systemctl enable --now cloudflared

if [ "$TUNNEL_ONLY" = 1 ]; then
  echo 'The tunnel is connected. The Google sign-in settings are unchanged.'; exit 0
fi
save_settings "$SETTINGS_JSON"
systemctl restart 3d-printer-management
PORT="$("$PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("port",8080))' "$CONFIG")"

# Does https://<host> really ask for a Google sign-in? (Asked like a visitor without one; the tunnel can take a
# minute to come up.)
echo "Checking https://$HOST ..."
RESULT=''
for _ in $(seq 1 12); do
  RESULT="$(curl -s -o /dev/null -m 15 -w '%{http_code} %{redirect_url}' "https://$HOST/" || true)"
  case "$RESULT" in
    30[1-8]\ *cloudflareaccess.com*) break ;;
    530*|502*|000*) sleep 5 ;;
    *) break ;;
  esac
done
echo
case "$RESULT" in
  30[1-8]\ *"//$TEAM/"*)
    echo "OK: https://$HOST asks for a Google sign-in. Open it and sign in." ;;
  30[1-8]\ *cloudflareaccess.com*)
    echo "WARNING: https://$HOST asks for a sign-in with another Zero Trust team than $TEAM: check --team." ;;
  530*|502*|000*)
    echo "WARNING: Cloudflare can't reach the Pi yet ($RESULT). Check the tunnel's public hostname $HOST points at http://localhost:$PORT,"
    echo "         and sudo systemctl status cloudflared." ;;
  *)
    echo "WARNING: https://$HOST answered without a Google sign-in ($RESULT)."
    echo "         Add a Cloudflare Access application for $HOST (step 3 of docs/cloudflare-access.md). The dashboard refuses"
    echo "         its API without Access's token meanwhile, but don't rely on it." ;;
esac
echo "The Pi's own addresses can use: $("$PYTHON" -c 'import json,sys;print(json.loads(sys.argv[1])["lan"])' "$SETTINGS_JSON") (change it in Settings & help -> Google sign-in)."
