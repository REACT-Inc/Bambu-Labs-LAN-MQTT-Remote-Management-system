"""The slicing worker installer and management command, served by the Pi at /slicer/worker/install.sh (docs/slicer.md).

On a fresh Ubuntu Server / Debian computer (no desktop needed):

    curl -fsSL http://PI:8080/slicer/worker/install.sh | sudo bash -s -- --server http://PI:8080 --code SETUP-CODE

It checks the computer, installs OrcaSlicer ORCA_VERSION from its official GitHub release (the Linux AppImage, extracted,
so FUSE isn't needed) and the worker from the Pi, registers it with the setup code, runs a real test slice, installs the
orca-worker systemd service (memory-limited, restarted if it stops), keeps a laptop awake with its lid closed, and
leaves the `slicer-worker` command for later: status, logs, test, update, restart, doctor, uninstall.

The script is kept here (not as a .sh file) because release ZIPs only carry .py files in package folders.
"""

ORCA_VERSION = 'v2.4.2'   # the OrcaSlicer release the worker is tested with (its command line and presets)

SCRIPT = r'''#!/usr/bin/env bash
# Slicing worker installer and management command for 3D Printer Management (docs/slicer.md).
# Installed as /usr/local/bin/slicer-worker; run "slicer-worker help".
set -euo pipefail

ORCA_VERSION_DEFAULT="__ORCA_VERSION__"
CONFIG=/etc/orca-worker/config.json
APP=/opt/orca-worker
DATA=/var/lib/orca-worker
UNIT=/etc/systemd/system/orca-worker.service
TIMER=/etc/systemd/system/slicer-worker-update.timer
LOGIND=/etc/systemd/logind.conf.d/50-slicer-worker.conf
SLEEP_TARGETS="sleep.target suspend.target hibernate.target hybrid-sleep.target"

usage() {
  cat <<'HELP'
slicer-worker: install and manage an OrcaSlicer slicing worker for 3D Printer Management.

  slicer-worker install --server http://PI:8080 --code SETUP-CODE [options]
                         set up this computer (the dashboard's Slicer -> Workers -> Add a worker gives the command)
  slicer-worker status   is it running, registered, which OrcaSlicer, the last log lines
  slicer-worker logs     follow the worker's log (Ctrl+C to stop)
  slicer-worker test     slice a 20 mm test cube with OrcaSlicer
  slicer-worker update   update the worker (and OrcaSlicer, when the Pi wants a newer one) from the Pi
  slicer-worker restart  restart the service
  slicer-worker doctor   check this computer: system, memory, disk, the Pi, GitHub, the service
  slicer-worker uninstall [--purge]
                         remove the service, worker and OrcaSlicer (--purge also removes its registration and data)

Install options:
  --server URL         the dashboard's address, e.g. http://192.168.1.10:8080 (asked for if missing)
  --code CODE          the setup code from the dashboard (needed the first time; asked for if missing)
  --name NAME          the name shown in the dashboard (default: this computer's hostname)
  --memory-max SIZE    the most memory the worker may use (default: 6G; e.g. 12G on a 16 GB computer)
  --auto-update        update from the Pi every night at 04:30, when the worker is idle
  --no-keep-awake      don't stop this laptop sleeping when its lid is closed
  --orca-version V     a different OrcaSlicer release tag (default: the one the Pi was tested with)
  --orca-url URL       download the OrcaSlicer Linux AppImage from here instead of GitHub
  --yes                don't ask anything (fail instead)
HELP
}

say()  { echo "→ $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ! $*" >&2; WARNINGS=$((WARNINGS + 1)); }
fail() { echo "✗ $*" >&2; exit 1; }
WARNINGS=0

need_root() { [ "$(id -u)" -eq 0 ] || fail "Run it as root: sudo slicer-worker $COMMAND (or put sudo before bash)."; }

ask() {   # ask VARIABLE "question" : reads from the terminal, also when this script is piped into bash
  local answer=""
  if [ "$YES" = 1 ] || ! { true </dev/tty; } 2>/dev/null; then return 1; fi
  read -r -p "$2 " answer </dev/tty || true
  [ -n "$answer" ] || return 1
  printf -v "$1" '%s' "$answer"
}

config_value() {   # config_value KEY : a value from the worker's config, or nothing
  [ -r "$CONFIG" ] || return 0
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' "$CONFIG" "$1" 2>/dev/null || true
}

normalise_server() {   # 192.168.1.10 -> http://192.168.1.10:8080
  local s="${1%/}"
  case "$s" in http://*|https://*) ;; *) s="http://$s";; esac
  case "${s#*://}" in *:*) ;; *) s="$s:8080";; esac
  echo "$s"
}

os_field() { ( . /etc/os-release 2>/dev/null; eval "echo \"\${$1:-}\"" ); }   # in a subshell: os-release sets NAME, VERSION...

busy() { compgen -G "$DATA/job-*" >/dev/null 2>&1; }   # a job's temporary folder exists while it slices

# ---- checks ----

check_system() {
  say "Checking this computer"
  [ "$(uname -m)" = "x86_64" ] || fail "OrcaSlicer's Linux build needs an x86_64 (Intel/AMD) computer; this one is $(uname -m)."
  [ -r /etc/os-release ] || fail "Unknown Linux distribution (no /etc/os-release)."
  local id like pretty version major
  id=$(os_field ID); like=$(os_field ID_LIKE); pretty=$(os_field PRETTY_NAME); version=$(os_field VERSION_ID)
  case "$id:$like" in
    ubuntu:*|debian:*|*:*debian*|*:*ubuntu*) ;;
    *) fail "Only Ubuntu and Debian are supported (this is ${pretty:-unknown})." ;;
  esac
  major="${version%%.*}"
  if { [ "$id" = ubuntu ] && [ "${major:-0}" -lt 24 ]; } || { [ "$id" = debian ] && [ "${major:-0}" -lt 13 ]; }; then
    warn "${pretty:-This system} is older than Ubuntu 24.04 / Debian 13: OrcaSlicer may not run (the test slice will tell)."
  else
    ok "${pretty:-$id}"
  fi
  command -v systemctl >/dev/null && [ -d /run/systemd/system ] || fail "systemd is needed to run the worker as a service."
  local ram_mb=$(( $(awk '/^MemTotal:/ {print $2}' /proc/meminfo) / 1024 ))
  if [ "$ram_mb" -lt 3500 ]; then warn "Only ${ram_mb} MB of memory: 4 GB or more is recommended, 8 GB for big plates."; else ok "${ram_mb} MB of memory, $(nproc) CPU cores"; fi
  local free_mb=$(df -Pm /opt | awk 'NR==2 {print $4}')
  [ "$free_mb" -ge 2500 ] || fail "Only ${free_mb} MB free on /opt: OrcaSlicer needs about 2.5 GB."
  ok "${free_mb} MB free disk"
}

check_server() {
  curl -fsS -m 15 -o /dev/null "$SERVER/slicer/worker/install.sh" \
    || fail "Can't reach the dashboard at $SERVER. Check the address and port, and that this computer is on the same network (or VPN)."
  ok "Dashboard reachable at $SERVER"
}

server_time() {   # the dashboard's clock (its HTTP Date header) as seconds since 1970, or nothing
  local date
  date=$(curl -fsSI -m 15 "$SERVER/slicer/worker/install.sh" 2>/dev/null | tr -d '\r' | sed -n 's/^[Dd]ate: //p' | head -n1)
  [ -n "$date" ] && date -u -d "$date" +%s 2>/dev/null || true
}

clock_offset() {   # seconds this computer's clock is behind (+) or ahead (-) of the dashboard's, or nothing
  local server
  server=$(server_time)
  [ -n "$server" ] && echo $(( server - $(date -u +%s) )) || true
}

check_clock() {   # A wrong clock makes apt refuse Ubuntu's package lists ("not valid yet") and HTTPS fail.
  local offset waited=0
  offset=$(clock_offset)
  if [ -z "$offset" ]; then warn "Couldn't read the dashboard's clock: skipping the clock check."; return; fi
  if [ "${offset#-}" -le 120 ]; then ok "Clock is right"; return; fi
  say "This computer's clock is $(( ${offset#-} / 60 )) minutes $( [ "$offset" -gt 0 ] && echo behind || echo ahead ): fixing it"
  timedatectl set-ntp true >/dev/null 2>&1 || true   # automatic time sync (systemd-timesyncd)
  while [ "$waited" -lt 30 ]; do
    [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] && break
    sleep 2; waited=$((waited + 2))
  done
  offset=$(clock_offset)
  if [ -n "$offset" ] && [ "${offset#-}" -gt 120 ]; then   # no time server reachable: take the dashboard's time
    date -u -s "@$(server_time)" >/dev/null
    command -v hwclock >/dev/null && hwclock --systohc >/dev/null 2>&1 || true
    warn "Time sync didn't work here (are time servers blocked?): set the clock from the dashboard's instead."
  else
    ok "Clock fixed by automatic time sync"
  fi
}

# ---- install / update ----

install_packages() {
  say "Installing packages"
  export DEBIAN_FRONTEND=noninteractive
  local apt="apt-get -o DPkg::Lock::Timeout=600 -qq"
  if ! $apt update; then
    fail "apt-get update failed (see above). Check this computer's clock (timedatectl), its internet access and /etc/apt/sources.list, then run this again."
  fi
  pick() { for p in "$@"; do if apt-cache show "$p" >/dev/null 2>&1; then echo "$p"; return; fi; done; }
  local packages="python3 curl ca-certificates xvfb xauth libgl1 libegl1 libglu1-mesa libmspack0 libgstreamer1.0-0 gstreamer1.0-plugins-base"
  packages="$packages $(pick libgtk-3-0t64 libgtk-3-0) $(pick libwebkit2gtk-4.1-0) $(pick libfuse2t64 libfuse2)"
  # shellcheck disable=SC2086
  $apt install -y --no-install-recommends $packages >/dev/null
  ok "Packages installed"
}

install_orca() {
  if [ "$(cat /opt/orcaslicer/.orca-version 2>/dev/null)" = "$ORCA_VERSION" ] && [ -x /opt/orcaslicer/AppRun ]; then
    ok "OrcaSlicer $ORCA_VERSION already installed"
    return
  fi
  say "Downloading OrcaSlicer $ORCA_VERSION"
  if [ -z "$ORCA_URL" ]; then
    # The Ubuntu 24.04 build (glibc 2.39), or the 22.04 one on older systems when the release has it.
    local build=Ubuntu2404
    if [ "$(os_field ID):$(os_field VERSION_ID)" = "ubuntu:22.04" ]; then build=Ubuntu2204; fi
    ORCA_URL=$(curl -fsSL -m 30 "https://api.github.com/repos/OrcaSlicer/OrcaSlicer/releases/tags/$ORCA_VERSION" | python3 -c '
import json, sys
assets = [a["browser_download_url"] for a in json.load(sys.stdin).get("assets", []) if a["name"].endswith(".AppImage")]
best = [a for a in assets if sys.argv[1] + "_V" in a] or [a for a in assets if "Ubuntu2404_V" in a] or [a for a in assets if "Linux" in a]
print(best[0] if best else "")' "$build") || true
    [ -n "$ORCA_URL" ] || fail "Could not find OrcaSlicer $ORCA_VERSION's Linux AppImage on GitHub. Download it elsewhere and pass its link with --orca-url."
  fi
  local work
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
  curl -fL --retry 3 --progress-bar -o "$work/orca.AppImage" "$ORCA_URL" || fail "Downloading OrcaSlicer failed."
  chmod +x "$work/orca.AppImage"
  (cd "$work" && ./orca.AppImage --appimage-extract >/dev/null) || fail "Could not unpack the OrcaSlicer AppImage."
  rm -rf "/opt/orcaslicer-$ORCA_VERSION"
  mv "$work/squashfs-root" "/opt/orcaslicer-$ORCA_VERSION"
  chmod -R a+rX "/opt/orcaslicer-$ORCA_VERSION"
  echo "$ORCA_VERSION" > "/opt/orcaslicer-$ORCA_VERSION/.orca-version"
  ln -sfn "/opt/orcaslicer-$ORCA_VERSION" /opt/orcaslicer
  rm -rf "$work"; trap - EXIT
  for old in /opt/orcaslicer-*; do   # older OrcaSlicer versions this script installed
    if [ "$old" != "/opt/orcaslicer-$ORCA_VERSION" ] && [ -f "$old/.orca-version" ]; then rm -rf "$old"; fi
  done
  ok "OrcaSlicer $ORCA_VERSION in /opt/orcaslicer"
}

install_worker() {
  say "Installing the worker from $SERVER"
  id orcaworker >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin orcaworker
  install -d -o orcaworker -g orcaworker -m 0750 "$DATA"
  install -d -o root -g orcaworker -m 0750 /etc/orca-worker
  install -d -m 0755 "$APP"
  local work
  work=$(mktemp -d)
  curl -fsSL -m 60 "$SERVER/slicer/worker/bundle.tar.gz" -o "$work/bundle.tar.gz" || fail "Could not download the worker from $SERVER."
  curl -fsSL -m 60 "$SERVER/slicer/worker/install.sh" -o "$work/slicer-worker" || fail "Could not download slicer-worker from $SERVER."
  tar -xzf "$work/bundle.tar.gz" -C "$APP"
  install -m 0755 "$work/slicer-worker" "$APP/slicer-worker"
  ln -sfn "$APP/slicer-worker" /usr/local/bin/slicer-worker
  rm -rf "$work"
  ok "Worker $(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import worker; print(worker.VERSION)' "$APP" 2>/dev/null) in $APP, command: slicer-worker"
}

register() {
  if [ -n "$CODE" ]; then
    say "Registering with $SERVER as \"$NAME\""
    python3 "$APP/worker.py" --config "$CONFIG" enroll --server "$SERVER" --code "$CODE" --name "$NAME" --orca-version "$ORCA_VERSION"
  fi
  chown root:orcaworker "$CONFIG"; chmod 0640 "$CONFIG"
  python3 - "$CONFIG" "$ORCA_VERSION" "$SERVER" <<'PYEOF'
import json, sys
path, version, server = sys.argv[1:4]
config = json.load(open(path))
config.update(orca_version=version, orca=["/opt/orcaslicer/AppRun"], profiles="/opt/orcaslicer/resources/profiles",
              work_dir="/var/lib/orca-worker", server=server)
json.dump(config, open(path, "w"), indent=2)
PYEOF
}

test_slice() {
  say "Test slice (a 20 mm cube)"
  runuser -u orcaworker -- env HOME="$DATA" python3 "$APP/worker.py" --config "$CONFIG" test-slice \
    || fail "OrcaSlicer could not slice the test cube: see the output above, then run: sudo slicer-worker doctor"
}

install_service() {
  say "Installing the orca-worker service"
  cat > "$UNIT" <<UNITEOF
[Unit]
Description=OrcaSlicer slicing worker for 3D Printer Management
After=network-online.target
Wants=network-online.target

[Service]
User=orcaworker
Group=orcaworker
Environment=HOME=$DATA
ExecStart=/usr/bin/python3 $APP/worker.py --config $CONFIG run
Restart=always
RestartSec=5
MemoryMax=$MEMORY_MAX
TasksMax=1024
Nice=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
UNITEOF
  systemctl daemon-reload
  systemctl enable orca-worker.service >/dev/null 2>&1
  systemctl restart orca-worker.service
  sleep 3
  systemctl is-active --quiet orca-worker.service || { journalctl -u orca-worker -n 30 --no-pager; fail "The orca-worker service did not start."; }
  ok "orca-worker service running"
}

keep_awake() {
  say "Keeping this computer awake (lid closed, idle)"
  install -d /etc/systemd/logind.conf.d
  cat > "$LOGIND" <<'LOGINDEOF'
# Written by slicer-worker: a slicing worker must keep running with its lid closed.
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
LOGINDEOF
  # shellcheck disable=SC2086
  systemctl mask $SLEEP_TARGETS >/dev/null 2>&1 || true
  systemctl kill -s HUP systemd-logind >/dev/null 2>&1 || true   # reloads logind.conf without ending anyone's session
  ok "It won't sleep or suspend, also with the lid closed"
}

auto_update() {
  cat > /etc/systemd/system/slicer-worker-update.service <<'SVCEOF'
[Unit]
Description=Update the slicing worker from the dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/slicer-worker update --if-idle
SVCEOF
  cat > "$TIMER" <<'TIMEREOF'
[Unit]
Description=Update the slicing worker every night

[Timer]
OnCalendar=*-*-* 04:30
RandomizedDelaySec=30min
Persistent=true

[Install]
WantedBy=timers.target
TIMEREOF
  systemctl daemon-reload
  systemctl enable --now slicer-worker-update.timer >/dev/null 2>&1
  ok "Updates every night at about 04:30 when idle (sudo systemctl disable --now slicer-worker-update.timer to stop)"
}

cmd_install() {
  need_root
  if [ -z "$SERVER" ]; then SERVER=$(config_value server); fi
  if [ -z "$SERVER" ]; then
    ask SERVER "Dashboard address (for example 192.168.1.10:8080):" || fail "--server is required, for example --server http://192.168.1.10:8080"
  fi
  SERVER=$(normalise_server "$SERVER")
  if [ -z "$CODE" ] && [ -z "$(config_value token)" ]; then
    ask CODE "Setup code (dashboard: Slicer -> Workers -> Add a worker):" \
      || fail "--code is required the first time: make a setup code in the dashboard (Slicer -> Workers -> Add a worker)."
  fi
  [ -n "$NAME" ] || NAME="$(hostname)"
  check_system
  check_server
  check_clock             # before apt: a wrong clock makes it refuse the package lists
  install_packages
  install_orca
  install_worker
  register
  test_slice
  install_service
  if [ "$KEEP_AWAKE" = 1 ]; then keep_awake; fi
  if [ "$AUTO_UPDATE" = 1 ]; then auto_update; fi
  echo
  echo "✓ Slicing worker ready: it shows in the dashboard under Slicer -> Workers."
  echo "  Manage it with: sudo slicer-worker status | logs | test | update | doctor | uninstall"
  if [ "$WARNINGS" -gt 0 ]; then echo "  ($WARNINGS warning(s) above.)"; fi
}

cmd_update() {
  need_root
  [ -r "$CONFIG" ] || fail "This computer isn't set up yet: run slicer-worker install first."
  SERVER=$(normalise_server "${SERVER:-$(config_value server)}")
  [ -n "$SERVER" ] || fail "No dashboard address in $CONFIG: pass --server."
  if [ "$IF_IDLE" = 1 ] && busy; then echo "The worker is slicing: not updating now."; exit 0; fi
  say "Updating from $SERVER"
  # Run the dashboard's newest version of this script, so its OrcaSlicer version and steps are current.
  local newest
  newest=$(mktemp)
  curl -fsSL -m 60 "$SERVER/slicer/worker/install.sh" -o "$newest" || fail "Can't reach the dashboard at $SERVER."
  # Keep this computer's choices: memory limit, keep-awake and nightly updates, unless given again.
  if [ "$MEMORY_SET" = 0 ] && [ -f "$UNIT" ]; then MEMORY_MAX=$(sed -n 's/^MemoryMax=//p' "$UNIT" | head -n1); MEMORY_MAX=${MEMORY_MAX:-6G}; fi
  local args=(install --server "$SERVER" --memory-max "$MEMORY_MAX" --yes)
  if [ "$KEEP_AWAKE" = 0 ] || [ ! -f "$LOGIND" ]; then args+=(--no-keep-awake); fi
  if [ "$AUTO_UPDATE" = 1 ] || [ -f "$TIMER" ]; then args+=(--auto-update); fi
  if [ "$ORCA_VERSION_SET" = 1 ]; then args+=(--orca-version "$ORCA_VERSION"); fi
  if [ -n "$ORCA_URL" ]; then args+=(--orca-url "$ORCA_URL"); fi
  exec bash "$newest" "${args[@]}"
}

cmd_status() {
  local active id orca
  active=$(systemctl is-active orca-worker.service 2>/dev/null || true)
  id=$(config_value worker_id)
  orca=$(cat /opt/orcaslicer/.orca-version 2>/dev/null || echo "not installed")
  echo "Service:     ${active:-not installed}"
  echo "Registered:  ${id:-no}${id:+ with $(config_value server)}"
  echo "OrcaSlicer:  $orca"
  echo "Worker:      $(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import worker; print(worker.VERSION)' "$APP" 2>/dev/null || echo "not installed")"
  echo "Slicing now: $(busy && echo yes || echo no)"
  echo "Memory max:  $(systemctl show -p MemoryMax --value orca-worker.service 2>/dev/null | awk '{ if ($1 ~ /^[0-9]+$/) printf "%.1f GB", $1/1073741824; else print $1 }')"
  echo "Auto-update: $( [ -f "$TIMER" ] && echo "on (nightly)" || echo off)"
  echo "Keep awake:  $( [ -f "$LOGIND" ] && echo on || echo off)"
  echo
  journalctl -u orca-worker -n 8 --no-pager 2>/dev/null || true
}

cmd_doctor() {
  local server
  check_system || true
  server=$(config_value server)
  if [ -n "${SERVER:-$server}" ]; then
    SERVER=$(normalise_server "${SERVER:-$server}"); check_server
    local offset; offset=$(clock_offset)
    if [ -n "$offset" ] && [ "${offset#-}" -gt 120 ]; then warn "The clock is ${offset#-} seconds off the dashboard's: run sudo timedatectl set-ntp true"; else ok "Clock is right"; fi
  else
    warn "Not registered with a dashboard yet."
  fi
  if curl -fs -m 15 -o /dev/null https://api.github.com/; then ok "GitHub reachable (for OrcaSlicer downloads)"; else warn "Can't reach GitHub: updating OrcaSlicer needs --orca-url."; fi
  if [ -x /opt/orcaslicer/AppRun ]; then ok "OrcaSlicer $(cat /opt/orcaslicer/.orca-version 2>/dev/null) installed"; else warn "OrcaSlicer isn't installed."; fi
  for lib in libwebkit2gtk-4.1.so.0 libgtk-3.so.0 libEGL.so.1; do
    if ldconfig -p | grep -q "$lib"; then ok "$lib"; else warn "$lib is missing: run sudo slicer-worker update"; fi
  done
  if command -v xvfb-run >/dev/null; then ok "xvfb-run"; else warn "xvfb-run is missing: run sudo slicer-worker update"; fi
  if systemctl is-active --quiet orca-worker.service; then ok "orca-worker service running"; else warn "orca-worker service is not running: sudo journalctl -u orca-worker -n 50"; fi
  if [ -n "$(config_value token)" ]; then ok "Registered as $(config_value worker_id)"; else warn "Not registered: run slicer-worker install with --code."; fi
  echo
  if [ "$WARNINGS" -eq 0 ]; then echo "✓ Everything looks fine."; else echo "$WARNINGS problem(s) found."; fi
}

cmd_uninstall() {
  need_root
  if [ "$YES" != 1 ] && [ -t 0 ]; then
    local sure=""
    read -r -p "Remove the slicing worker and OrcaSlicer from this computer? [y/N] " sure
    case "$sure" in y|Y|yes) ;; *) echo "Nothing removed."; exit 0;; esac
  fi
  say "Removing the slicing worker"
  systemctl disable --now orca-worker.service slicer-worker-update.timer >/dev/null 2>&1 || true
  rm -f "$UNIT" "$TIMER" /etc/systemd/system/slicer-worker-update.service
  if [ -f "$LOGIND" ]; then
    rm -f "$LOGIND"
    # shellcheck disable=SC2086
    systemctl unmask $SLEEP_TARGETS >/dev/null 2>&1 || true
    systemctl kill -s HUP systemd-logind >/dev/null 2>&1 || true
  fi
  systemctl daemon-reload
  rm -rf "$APP" /usr/local/bin/slicer-worker /opt/orcaslicer
  for dir in /opt/orcaslicer-*; do if [ -f "$dir/.orca-version" ]; then rm -rf "$dir"; fi; done
  if [ "$PURGE" = 1 ]; then
    rm -rf /etc/orca-worker "$DATA"
    userdel orcaworker >/dev/null 2>&1 || true
    echo "✓ Removed, including its registration. Remove it in the dashboard too (Slicer -> Workers -> Remove)."
  else
    echo "✓ Removed. Its registration is kept in /etc/orca-worker, so installing again needs no new code (--purge removes it)."
  fi
}

# ---- arguments ----

COMMAND=install
case "${1:-}" in
  install|update|status|logs|test|restart|doctor|uninstall|help) COMMAND="$1"; shift;;
  -h|--help) COMMAND=help; shift;;
esac
SERVER=""; CODE=""; NAME=""; ORCA_VERSION="$ORCA_VERSION_DEFAULT"; ORCA_VERSION_SET=0; ORCA_URL=""; MEMORY_MAX="6G"; MEMORY_SET=0
KEEP_AWAKE=1; AUTO_UPDATE=0; YES=0; PURGE=0; IF_IDLE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="${2:-}"; shift 2;;
    --code) CODE="${2:-}"; shift 2;;
    --name) NAME="${2:-}"; shift 2;;
    --orca-version) ORCA_VERSION="${2:-}"; ORCA_VERSION_SET=1; shift 2;;
    --orca-url) ORCA_URL="${2:-}"; shift 2;;
    --memory-max) MEMORY_MAX="${2:-}"; MEMORY_SET=1; shift 2;;
    --auto-update) AUTO_UPDATE=1; shift;;
    --no-keep-awake) KEEP_AWAKE=0; shift;;
    --if-idle) IF_IDLE=1; shift;;
    --purge) PURGE=1; shift;;
    --yes|-y) YES=1; shift;;
    -h|--help) COMMAND=help; shift;;
    *) echo "Unknown option: $1 (see: slicer-worker help)" >&2; exit 2;;
  esac
done
if ! [[ "$MEMORY_MAX" =~ ^[0-9]+[KMGT]?$ ]]; then fail "--memory-max must look like 6G or 8192M."; fi
if ! [[ "$ORCA_VERSION" =~ ^v[0-9][0-9A-Za-z.-]*$ ]]; then fail "--orca-version must be a release tag like v2.4.2."; fi

case "$COMMAND" in
  help) usage;;
  install) cmd_install;;
  update) cmd_update;;
  status) cmd_status;;
  logs) exec journalctl -u orca-worker -f -n 50;;
  test) need_root; test_slice; echo "✓ OrcaSlicer works.";;
  restart) need_root; systemctl restart orca-worker.service; systemctl is-active orca-worker.service;;
  doctor) cmd_doctor;;
  uninstall) cmd_uninstall;;
esac
'''


def install_script():
    return SCRIPT.replace('__ORCA_VERSION__', ORCA_VERSION)
