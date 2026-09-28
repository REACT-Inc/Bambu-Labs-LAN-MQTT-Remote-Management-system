# Installation

This guide installs 3D Printer Management as a systemd service on a Raspberry Pi, or on [Ubuntu or another Linux distribution](#ubuntu-and-other-linux-distributions). For settings after installation, see [Configuration](configuration.md).

- [Requirements](#requirements)
- [Ubuntu and other Linux distributions](#ubuntu-and-other-linux-distributions)
- [Choose how you'll reach the dashboard](#choose-how-youll-reach-the-dashboard)
- [Install](#install)
- [First login](#first-login)
- [Migrating from the original Discord bot](#migrating-from-the-original-discord-bot)
- [Demo mode](#demo-mode)
- [Optional: allow rebooting the Pi from Discord or the dashboard](#optional-allow-rebooting-the-pi-from-discord-or-the-dashboard)
- [What the installer changes](#what-the-installer-changes)
- [Reinstalling, rolling back and uninstalling](#reinstalling-rolling-back-and-uninstalling)

## Requirements

| Requirement | Details |
|---|---|
| Hardware | Raspberry Pi 4 or 5 (or another machine running Raspberry Pi OS / Debian) |
| Operating system | Raspberry Pi OS Bookworm or newer, with systemd |
| Python | 3.11 or newer (installed by the installer from the OS packages) |
| Network to printers | The Pi must reach each printer on the LAN: MQTT over TLS on **8883**, FTPS on **990** (plus its data connection), and the camera port (**6000** for A1-family JPEG cameras, **322** for RTSP cameras such as the H2D) |
| Printer settings | LAN mode / developer mode as required by your firmware, and each printer's **IP address, serial number and access code** |
| Internet (during install) | To download OS packages and Python packages from PyPI |
| Discord (optional) | A bot token and the IDs of the servers (guilds) it may be used in. Without a token the dashboard still works. |

The installer installs `python3` (with `venv`), `ffmpeg` and `sudo` using the system's package manager.

## Ubuntu and other Linux distributions

The same installer works on a regular Linux PC, mini PC, VM or server instead of a Raspberry Pi. It detects the distribution and installs what it needs.

| Distribution | Package manager | Notes |
|---|---|---|
| **Ubuntu 24.04+**, Linux Mint 22+, Pop!\_OS 24.04+ | apt | Ubuntu 22.04 is **not** supported (Python 3.10) |
| **Debian 12+**, Raspberry Pi OS Bookworm | apt | |
| **Fedora** 39+ | dnf | ffmpeg needs RPM Fusion for some cameras |
| **Arch Linux**, Manjaro, EndeavourOS | pacman | The installer runs `pacman -Syu`, which upgrades the system |
| **openSUSE Tumbleweed** | zypper | ffmpeg needs the Packman repository for some cameras |
| Other systemd distributions | — | Install Python 3.11+ (with `venv`), `sudo` and optionally `ffmpeg` yourself; the installer skips package installation |

- **systemd is required.** It runs the service and the web updater. WSL without systemd, Docker containers and Alpine/OpenRC aren't supported.
- **Python 3.11 or newer.** The installer checks this before changing anything.
- **Network:** the computer must reach the printers' LAN, the same as a Pi (see the ports above).
- **Dashboard address:** if Tailscale isn't connected and no `PM_LISTEN_IP` is given, the installer stops and suggests this computer's LAN address. `PM_LISTEN_IP=127.0.0.1` limits the dashboard to this computer.
- **Firewall:** if `ufw` or `firewalld` is active, the installer prints the command that opens the dashboard port, for example `sudo ufw allow 8080/tcp` or `sudo firewall-cmd --permanent --add-port=8080/tcp && sudo firewall-cmd --reload`. It doesn't change your firewall.
- **ffmpeg** is only needed for camera snapshots from printers with an RTSP camera, such as the H2D. If it can't be installed, the installer warns and carries on.
- **Leave the computer on.** Laptops and desktops shouldn't suspend while prints are queued or running.
- **Everything else is the same as on the Pi:** file locations, `systemctl`/`journalctl` commands and updates. Screens that say "Pi" (for example **Reboot Pi**, `/server`, `/reboot`) mean this computer.

## Choose how you'll reach the dashboard

The dashboard never listens on every network interface. It always listens on `127.0.0.1` (the Pi itself), plus **one** address you choose:

| Option | How | Who can open the dashboard |
|---|---|---|
| **Tailscale** (recommended) | Install and sign in to Tailscale on the Pi before running the installer. The installer picks up the Pi's Tailscale IPv4 address automatically. | Devices on your tailnet that your Tailscale policy allows to reach the Pi on TCP 8080 |
| **LAN address** | Run the installer as `sudo PM_LISTEN_IP=192.168.1.50 bash install.sh` (use the Pi's own LAN IP) | Anyone on that network who knows the password |

If Tailscale isn't connected and `PM_LISTEN_IP` isn't set, the installer stops without changing anything. The installer never changes Tailscale, your tailnet policy or firewall rules.

The dashboard uses plain HTTP on port 8080, protected by a password. Use Tailscale, or keep it on a trusted network. Don't forward the port to the internet.

## Install

1. **Download and extract** the release ZIP (`3d-printer-management.zip`) on the Pi. It extracts to a `printer-management` folder.
2. **If you're migrating** from the original bot, make sure its source is at `/tmp/printer_discord_bot.py`, or pass `--old <path>`. See [Migrating](#migrating-from-the-original-discord-bot).
3. **Run the installer:**
   ```bash
   cd printer-management
   sudo bash install.sh
   ```
   Useful options (you can combine them):

   | Option | Effect |
   |---|---|
   | `PM_LISTEN_IP=<ip>` (environment variable) | Listen on this address instead of the Tailscale address |
   | `--old /path/to/printer_discord_bot.py` | Import settings from a bot file somewhere other than `/tmp` |
   | `--demo` | Create a demo configuration with no real printers. See [Demo mode](#demo-mode). |
   | `--enable-reboot` | Allow the Pi to be rebooted from Discord or the dashboard. See [below](#optional-allow-rebooting-the-pi-from-discord-or-the-dashboard). |

4. **Wait for the health check.** The installer validates the configuration and Discord command definitions, starts the service, and waits up to 15 seconds for the dashboard to answer. If it doesn't, it restores the previous installation and shows the last 30 log lines.
5. **Save the password.** The installer prints the dashboard address(es) and, on a first install, an **initial dashboard password**.

## First login

1. **Sign in:** open the printed address, for example `http://100.x.y.z:8080`, from a device that can reach it, and sign in with the initial password.
2. **Change the password:** go to **Settings & help → Dashboard access** and set a new password (at least 12 characters). This signs out every session.
3. **Set up Discord** (**Settings & help**):
   - **Channels:** set the notification and commands channels, or run `/setnotificationchannel` and `/setcommandschannel` in Discord.
   - **Approved user IDs:** add the Discord user IDs of people who may run admin commands without being server administrators.
4. **Check demo mode:** if the configuration was imported with `demo: true`, check the printer entries in `/etc/3d-printer-management/config.json`, then set `demo` to `false` and restart. See [Configuration](configuration.md).
5. **Test one printer first:** queue a small sliced file on one cleared printer before relying on the queue.

## Migrating from the original Discord bot

If `/tmp/printer_discord_bot.py` (or the file given with `--old`) exists, the installer reads these **literal** values from it without running it:

| Old setting | New setting |
|---|---|
| `DISCORD_BOT_TOKEN` | `discord_token` |
| `ALLOWED_GUILD_IDS` | `guild_ids` |
| `SETTINGS_USER_IDS` | `admin_user_ids` |
| `PRINTERS` | `printers` |
| `SETTINGS_FILE` | its channel settings are copied to `/var/lib/3d-printer-management/settings.json` |

How the import works:
- **Literal values only.** A setting built with code, such as reading an environment variable, is rejected with a message. Convert it to a literal first.
- **Printer checks.** Each printer needs a unique `name`. Live printers also need `ip`, `serial` and `access_code`.
- **Empty printer list.** If the old file has an empty printer list (a temporary `PRINTERS=[]` override), the import keeps any earlier real printer list and turns on demo mode.
- **The old bot is replaced.** The installer stops and disables the old `printer-discord-bot` service once the new one is healthy. Don't run both at once.
- **A copy is kept.** The old file is backed up to `/var/backups/3d-printer-management/<timestamp>/legacy_bot.py`. `/tmp` may be cleared on reboot, so use this backup if you need the old file later.

To go back to the old bot while its source still exists:

```bash
sudo systemctl disable --now 3d-printer-management
sudo systemctl enable --now printer-discord-bot
```

## Demo mode

Demo mode runs the dashboard and Discord bot without connecting to any printer. Use it to try the system out or to test changes.

- **Fresh demo install:** run `sudo bash install.sh --demo` (Tailscale or `PM_LISTEN_IP` is still required).
- **Demo printers:** with no printers configured, two demo printers appear. You can supply your own demo data with `example_data` in `config.json`.
- **Demo jobs:** queued demo jobs run a short fake print, roughly 22 seconds with progress milestones. Pause, resume and stop act on the simulated job.
- **Demo controls:** printer controls only update the simulated data.
- **Going live:** demo jobs are tagged and can't be started after you switch to live mode. Remove them and queue new live jobs.

Demo mode is on when `"demo": true` is set, **or** when no printers are configured.

## Optional: allow rebooting the Pi from Discord or the dashboard

Rebooting the Pi is off by default. To turn it on, re-run the installer with:

```bash
sudo bash install.sh --enable-reboot
```

This does three things:
- sets `allow_host_reboot: true` in `config.json`
- installs a root-owned, no-argument helper `/usr/local/sbin/pm-host-reboot`
- adds `/etc/sudoers.d/printermanager-reboot`, so the service account can run only that helper

After that, `/reboot` and the dashboard **Server → Reboot Pi…** button work, but only for administrators and approved users, and only while no queue job is staging, waiting to start, printing or paused. A reboot interrupts everything else on the Pi too.

To turn it off again:
1. Set `allow_host_reboot` to `false` in `config.json`.
2. Delete `/etc/sudoers.d/printermanager-reboot`.
3. Restart the service.

## What the installer changes

| Path / item | Purpose |
|---|---|
| `/opt/3d-printer-management/` | Application code and its Python virtual environment (`venv/`) |
| `/etc/3d-printer-management/config.json` | Private configuration (owner `root`, group `printermanager`, mode 640) |
| `/var/lib/3d-printer-management/` | Data: queue database, uploads, settings, password hash, logs |
| `printermanager` system user | Runs the service. It has no login shell. |
| `3d-printer-management.service` | The main service (hardened: read-only system, private `/tmp`, writes only to the data folder) |
| `pm-web-update.path` / `.service` | Root-owned web updater. It watches for an approved update request. |
| `/usr/local/lib/pm-updater/`, `/var/lib/pm-updater/` | Updater code, state and backups |
| `/var/backups/3d-printer-management/<timestamp>/` | Backup of the previous code, config, service file and old bot, made on every install |

## Reinstalling, rolling back and uninstalling

**Reinstalling:** running `install.sh` again is safe. It keeps the existing `config.json`, data and dashboard password, backs up the current installation first, and rolls back if the new version fails its health check. To update, it's usually easier to use [dashboard or GitHub updates](updates.md).

**Uninstalling:** there's no uninstall script. To remove everything by hand:

```bash
sudo systemctl disable --now 3d-printer-management pm-web-update.path pm-web-update.service
sudo rm -f /etc/systemd/system/3d-printer-management.service /etc/systemd/system/pm-web-update.{path,service}
sudo systemctl daemon-reload
sudo rm -rf /opt/3d-printer-management /opt/3d-printer-management-releases /usr/local/lib/pm-updater
sudo rm -f /usr/local/sbin/pm-host-reboot /etc/sudoers.d/printermanager-reboot
# Only if you no longer need your data, configuration and backups:
sudo rm -rf /var/lib/3d-printer-management /var/lib/pm-updater /etc/3d-printer-management /var/backups/3d-printer-management
sudo userdel printermanager
```
