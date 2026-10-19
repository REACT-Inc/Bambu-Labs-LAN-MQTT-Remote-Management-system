"""The slicing worker installer, served by the Pi at /slicer/worker/install.sh (docs/slicer.md).

    curl -fsSL http://PI:8080/slicer/worker/install.sh | sudo bash -s -- --server http://PI:8080 --code SETUP-CODE

On Ubuntu Server / Debian (no desktop needed) it installs OrcaSlicer ORCA_VERSION from its official GitHub release
(the Linux AppImage, extracted, so FUSE isn't needed), the worker from the Pi, registers it with the setup code, runs a
real test slice, and installs the orca-worker systemd service (memory-limited, restarted if it stops). Running it
again updates the worker and, when ORCA_VERSION changed, OrcaSlicer; --code is then optional.
"""

ORCA_VERSION = 'v2.4.2'   # the OrcaSlicer release the worker is tested with (its command line and presets)

SCRIPT = r'''#!/usr/bin/env bash
# Slicing worker installer for 3D Printer Management (docs/slicer.md).
set -euo pipefail

SERVER=""; CODE=""; NAME="$(hostname)"; ORCA_VERSION="__ORCA_VERSION__"; ORCA_URL=""; MEMORY_MAX="6G"
while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="${2%/}"; shift 2;;
    --code) CODE="$2"; shift 2;;
    --name) NAME="$2"; shift 2;;
    --orca-version) ORCA_VERSION="$2"; shift 2;;
    --orca-url) ORCA_URL="$2"; shift 2;;
    --memory-max) MEMORY_MAX="$2"; shift 2;;
    *) echo "Unknown option: $1" >&2; exit 2;;
  esac
done

fail() { echo "✗ $*" >&2; exit 1; }
step() { echo "→ $*"; }

[ "$(id -u)" -eq 0 ] || fail "Run it as root (sudo)."
[ -n "$SERVER" ] || fail "--server is required, for example --server http://192.168.1.10:8080"
[ "$(uname -m)" = "x86_64" ] || fail "OrcaSlicer's Linux build needs an x86_64 computer (this one is $(uname -m))."
. /etc/os-release
case "${ID:-}:${ID_LIKE:-}" in
  ubuntu:*|debian:*|*:*debian*|*:*ubuntu*) ;;
  *) fail "Only Ubuntu and Debian are supported (this is ${PRETTY_NAME:-unknown})." ;;
esac

CONFIG=/etc/orca-worker/config.json
if [ -z "$CODE" ] && ! grep -q '"token": "[^"]' "$CONFIG" 2>/dev/null; then
  fail "--code is required the first time: make a setup code in the dashboard (Slicer → Workers → Add a worker)."
fi

step "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
pick() { for p in "$@"; do if apt-cache show "$p" >/dev/null 2>&1; then echo "$p"; return; fi; done; }
PACKAGES="python3 curl ca-certificates xvfb xauth libgl1 libegl1 libmspack0 libgstreamer1.0-0 gstreamer1.0-plugins-base"
PACKAGES="$PACKAGES $(pick libgtk-3-0t64 libgtk-3-0) $(pick libwebkit2gtk-4.1-0)"
apt-get install -y -qq --no-install-recommends $PACKAGES >/dev/null

if [ "$(cat /opt/orcaslicer/.orca-version 2>/dev/null)" != "$ORCA_VERSION" ]; then
  step "Downloading OrcaSlicer $ORCA_VERSION"
  if [ -z "$ORCA_URL" ]; then
    # The Ubuntu 24.04 build (glibc 2.39), or the 22.04 one on older systems when the release has it.
    BUILD=Ubuntu2404; [ "${ID:-}:${VERSION_ID:-}" = "ubuntu:22.04" ] && BUILD=Ubuntu2204
    ORCA_URL=$(curl -fsSL "https://api.github.com/repos/OrcaSlicer/OrcaSlicer/releases/tags/$ORCA_VERSION" | python3 -c '
import json, sys
assets = [a["browser_download_url"] for a in json.load(sys.stdin).get("assets", []) if a["name"].endswith(".AppImage")]
best = [a for a in assets if sys.argv[1] + "_V" in a] or [a for a in assets if "Ubuntu2404_V" in a] or [a for a in assets if "Linux" in a]
print(best[0] if best else "")' "$BUILD") || true
    [ -n "$ORCA_URL" ] || fail "Could not find OrcaSlicer $ORCA_VERSION's Linux AppImage on GitHub. Pass its link with --orca-url."
  fi
  WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
  curl -fL --progress-bar -o "$WORK/orca.AppImage" "$ORCA_URL"
  chmod +x "$WORK/orca.AppImage"
  (cd "$WORK" && ./orca.AppImage --appimage-extract >/dev/null) || fail "Could not unpack the OrcaSlicer AppImage."
  rm -rf "/opt/orcaslicer-$ORCA_VERSION"
  mv "$WORK/squashfs-root" "/opt/orcaslicer-$ORCA_VERSION"
  chmod -R a+rX "/opt/orcaslicer-$ORCA_VERSION"
  echo "$ORCA_VERSION" > "/opt/orcaslicer-$ORCA_VERSION/.orca-version"
  ln -sfn "/opt/orcaslicer-$ORCA_VERSION" /opt/orcaslicer
fi
[ -x /opt/orcaslicer/AppRun ] || fail "OrcaSlicer is not installed in /opt/orcaslicer."

step "Installing the worker"
id orcaworker >/dev/null 2>&1 || useradd --system --home-dir /var/lib/orca-worker --shell /usr/sbin/nologin orcaworker
install -d -o orcaworker -g orcaworker -m 0750 /var/lib/orca-worker
install -d -o root -g orcaworker -m 0750 /etc/orca-worker
install -d -m 0755 /opt/orca-worker
curl -fsSL "$SERVER/slicer/worker/bundle.tar.gz" | tar -xz -C /opt/orca-worker || fail "Could not download the worker from $SERVER."
PY="python3 /opt/orca-worker/worker.py --config $CONFIG"

if [ -n "$CODE" ]; then
  step "Registering with $SERVER"
  $PY enroll --server "$SERVER" --code "$CODE" --name "$NAME" --orca-version "$ORCA_VERSION"
fi
chown root:orcaworker "$CONFIG"; chmod 0640 "$CONFIG"
python3 - "$CONFIG" "$ORCA_VERSION" <<'PYEOF'
import json, sys
path, version = sys.argv[1], sys.argv[2]
config = json.load(open(path))
config.update(orca_version=version, orca=["/opt/orcaslicer/AppRun"], profiles="/opt/orcaslicer/resources/profiles",
              work_dir="/var/lib/orca-worker")
json.dump(config, open(path, "w"), indent=2)
PYEOF

step "Test slice (a 20 mm cube)"
sudo -u orcaworker env HOME=/var/lib/orca-worker $PY test-slice || fail "OrcaSlicer could not slice the test cube: see the output above."

step "Installing the orca-worker service"
cat > /etc/systemd/system/orca-worker.service <<UNIT
[Unit]
Description=OrcaSlicer slicing worker for 3D Printer Management
After=network-online.target
Wants=network-online.target

[Service]
User=orcaworker
Group=orcaworker
Environment=HOME=/var/lib/orca-worker
ExecStart=/usr/bin/python3 /opt/orca-worker/worker.py --config $CONFIG run
Restart=always
RestartSec=5
MemoryMax=$MEMORY_MAX
TasksMax=1024
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/orca-worker

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now orca-worker.service >/dev/null
systemctl restart orca-worker.service
sleep 3
systemctl is-active --quiet orca-worker.service || { journalctl -u orca-worker -n 30 --no-pager; fail "The orca-worker service did not start."; }
echo "✓ Slicing worker ready. It shows in the dashboard under Slicer → Workers."
'''


def install_script():
    return SCRIPT.replace('__ORCA_VERSION__', ORCA_VERSION)
