#!/usr/bin/env bash
# Linux distribution support for install.sh (sourced, not run directly).
# Raspberry Pi OS, Debian, Ubuntu and derivatives use apt; Fedora/RHEL use dnf;
# Arch/Manjaro use pacman; openSUSE uses zypper. Set PM_DRY_RUN=1 to print the
# package commands instead of running them, and PM_OS_RELEASE to test detection.

PM_MIN_PYTHON="3.11"

pm_detect_platform() {
  local file="${PM_OS_RELEASE:-/etc/os-release}" ID="" ID_LIKE="" PRETTY_NAME=""
  # shellcheck disable=SC1090
  if [ -r "$file" ]; then . "$file"; fi
  PM_OS_NAME="${PRETTY_NAME:-${ID:-unknown Linux}}"
  PM_PKG=unknown
  for candidate in $ID $ID_LIKE; do
    case "$candidate" in
      debian|ubuntu|raspbian|linuxmint|pop|elementary|zorin|kali) PM_PKG=apt; break ;;
      fedora|rhel|centos|rocky|almalinux|ol|amzn) PM_PKG=dnf; break ;;
      arch|manjaro|endeavouros|cachyos) PM_PKG=pacman; break ;;
      opensuse|opensuse-leap|opensuse-tumbleweed|suse|sles) PM_PKG=zypper; break ;;
    esac
  done
}

# Required packages, then optional ones (ffmpeg is only needed for RTSP printer cameras).
pm_required_packages() {
  case "$PM_PKG" in
    apt) echo "python3 python3-venv sudo" ;;
    dnf) echo "python3 sudo" ;;
    pacman) echo "python sudo" ;;
    zypper) echo "python3 sudo" ;;
  esac
}
pm_optional_packages() {
  case "$PM_PKG" in
    dnf) echo "ffmpeg-free" ;;  # Fedora's own build; RPM Fusion's "ffmpeg" also works
    apt|pacman|zypper) echo "ffmpeg" ;;
  esac
}

pm_run() {
  if [ "${PM_DRY_RUN:-0}" = 1 ]; then echo "+ $*"; else "$@"; fi
}

pm_install_packages() {
  local required optional
  required="$(pm_required_packages)"; optional="$(pm_optional_packages)"
  # Each step checks its own result: callers may run this where `set -e` does not apply.
  case "$PM_PKG" in
    apt)
      pm_run apt-get update || { echo "apt-get update failed."; return 1; }
      pm_run env DEBIAN_FRONTEND=noninteractive apt-get install -y $required || { echo "Could not install: $required"; return 1; } ;;
    dnf) pm_run dnf install -y $required || { echo "Could not install: $required"; return 1; } ;;
    # Arch only supports full upgrades (-Syu); installing after a bare -Sy can break the system.
    pacman) pm_run pacman -Syu --needed --noconfirm $required || { echo "Could not install: $required"; return 1; } ;;
    zypper) pm_run zypper --non-interactive install $required || { echo "Could not install: $required"; return 1; } ;;
    *)
      echo "Unrecognised distribution ($PM_OS_NAME): skipping package installation."
      echo "Make sure Python $PM_MIN_PYTHON+ (with venv), sudo and systemd are installed. ffmpeg is optional."
      return 0 ;;
  esac
  if [ -n "$optional" ] && ! pm_optional_install $optional; then
    echo "Warning: could not install ffmpeg. Everything works without it except camera snapshots"
    echo "from printers that use an RTSP camera (for example the X1 series and H2D)."
    case "$PM_PKG" in
      dnf) echo "On Fedora, enable RPM Fusion and run: sudo dnf install ffmpeg" ;;
      zypper) echo "On openSUSE, add the Packman repository and run: sudo zypper install ffmpeg" ;;
    esac
  fi
}
pm_optional_install() {
  case "$PM_PKG" in
    apt) pm_run env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
    dnf) pm_run dnf install -y "$@" || pm_run dnf install -y ffmpeg ;;
    pacman) pm_run pacman -S --needed --noconfirm "$@" ;;
    zypper) pm_run zypper --non-interactive install "$@" ;;
  esac
}

pm_check_systemd() {
  if [ ! -d /run/systemd/system ] || ! command -v systemctl >/dev/null; then
    echo "This installer needs systemd to run the service, and systemd is not running here."
    echo "Supported: Raspberry Pi OS, Ubuntu, Debian, Fedora, Arch and other systemd-based distributions."
    return 1
  fi
}

pm_check_python() {
  if ! command -v python3 >/dev/null; then
    echo "python3 was not found. Install Python $PM_MIN_PYTHON or newer and run the installer again."
    return 1
  fi
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Python $PM_MIN_PYTHON or newer is required; this system has $(python3 -V 2>&1)."
    echo "Use a newer release (for example Ubuntu 24.04+, Debian 12+, Fedora, Arch or Raspberry Pi OS Bookworm)."
    return 1
  fi
  if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
    echo "Python's venv module is missing. On Debian/Ubuntu: sudo apt install python3-venv"
    return 1
  fi
}

pm_nologin() {
  local shell
  for shell in "$(command -v nologin 2>/dev/null)" /usr/sbin/nologin /sbin/nologin /usr/bin/nologin; do
    if [ -n "$shell" ] && [ -x "$shell" ]; then echo "$shell"; return; fi
  done
  echo /bin/false
}

# The machine's main LAN IPv4 address, to suggest for PM_LISTEN_IP.
pm_suggest_ip() {
  local ip=""
  if command -v ip >/dev/null; then
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1)"
  fi
  if [ -z "$ip" ] && command -v hostname >/dev/null; then ip="$(hostname -I 2>/dev/null | awk '{print $1}')"; fi
  echo "$ip"
}

pm_firewall_hint() {
  local port="$1" host="$2"
  case "$host" in 127.*|"") return 0 ;; esac
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q 'Status: active'; then
    echo "ufw is active. To reach the dashboard from other computers: sudo ufw allow $port/tcp"
  elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    echo "firewalld is active. To reach the dashboard from other computers:"
    echo "  sudo firewall-cmd --permanent --add-port=$port/tcp && sudo firewall-cmd --reload"
  fi
}
