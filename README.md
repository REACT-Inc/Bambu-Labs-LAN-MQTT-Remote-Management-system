# 3D Printer Management

Self-hosted management for **Bambu Lab printers on your local network**. One service runs on a Raspberry Pi 4/5, **or any systemd-based Linux computer** (Ubuntu, Debian, Fedora, Arch, openSUSE…), and gives you:

- a **web dashboard**, reachable over Tailscale or your LAN: everything is set up and done here, from adding printers to running the queue
- a **desktop app for Windows laptops** (one `.exe`): the dashboard in its own window, with a tray icon for the printers and pop-up notifications
- optionally, a **Discord bot** with slash commands and notifications in your server (connected from the dashboard)
- optionally, alerts to **Home Assistant, ntfy and webhooks** (set up in the dashboard)

The dashboard and the bot share the same printer connections, print queues, files and history. Nothing runs in the cloud: the printers are controlled over LAN MQTT and FTPS, and the dashboard is served from the Pi.

| | |
|---|---|
| **Printers** | Tested on the H2D, A1 and A1 mini. Also knows the A2L, P1P, P1S, P2S, X1, X1 Carbon, X1E, X2D, H2D Pro, H2S and H2C (limits, fans, camera type), but those haven't been tested on real printers. See [supported printers](docs/configuration.md#supported-printers). |
| **Host** | Raspberry Pi 4/5 running Raspberry Pi OS, or another systemd-based Linux (Ubuntu 24.04+, Debian 12+, Fedora, Arch, openSUSE). Python 3.11+. See [Installation](docs/installation.md#ubuntu-and-other-linux-distributions). |
| **Version** | See `Updater/version.py` and the [changelog](CHANGELOG.md) |

## What it does

- **Printers:** live status, temperatures, progress, AMS/external spool, HMS and print errors (official Bambu descriptions), camera snapshots and low-frame-rate live view.
- **Shared print queue:** one queue per printer, fed from the dashboard (or Discord). Jobs are sliced `.3mf` uploads or files already on the printer.
- **Printer controls:** pause/resume/stop, light, nozzle/bed/chamber temperatures, speed profile, fans, and axis jogging.
- **Discord (optional):** about 50 slash commands and progress notifications with camera snapshots at every 10%. Printer and queue actions are open to every member with confirmation and logging, and admins can choose who may use each command.
- **Team tools:** shared notes, DM reminders, report-writer assignment and practice schedules, channel archiving, MeshCentral laptop commands, and Pi status/reboot.
- **Desktop app for Windows laptops:** the dashboard in its own window, plus a tray icon listing the printers (state, progress, time left, errors) and popping up the notifications: prints finished or failed, printer errors, AI alerts. It can start with Windows. See [Desktop app](docs/desktop-app.md).
- **Operations:**
  - updates from the dashboard or GitHub releases, with stable/beta/alpha channels and automatic rollback
  - error logs with error IDs
  - diagnostic reports
  - one-click problem reports sent as GitHub issues
- **Swapmod A1m:** plate-swap batch support for equipped A1 minis.

## Quick start (Raspberry Pi or Linux)

```bash
# On the Pi, in the extracted release folder:
cd printer-management
sudo bash install.sh                               # uses the Pi's Tailscale address
# or: sudo PM_LISTEN_IP=192.168.1.50 bash install.sh   (a LAN address instead)
```

Save the **initial dashboard password** the installer prints, open the address it shows (`http://<address>:8080`), and change the password under **Settings & help**. Then press **Add a printer** and enter each printer's IP address, serial number and access code: no Discord or file editing needed. Full instructions, demo mode and migrating from the original Discord bot are in the [installation guide](docs/installation.md).

On Windows laptops, you can also use the [desktop app](docs/desktop-app.md): download `3d-printer-management-desktop.exe` from the same release and enter the same address.

## Documentation

The guides below are in the `docs/` folder of the [GitHub repository](https://github.com/REACT-Inc/Bambu-Labs-LAN-MQTT-Remote-Management-system). Release ZIPs include only this README, the changelog and the files marked *(in release)*.

| Guide | Covers |
|---|---|
| [Installation](docs/installation.md) | Requirements, installing, network access, first login, demo mode, migrating from the old bot, uninstalling |
| [Configuration](docs/configuration.md) | Every `config.json` setting, printer entries, files and folders, service commands, password reset |
| [Web dashboard](docs/dashboard.md) | Each tab and what it does |
| [Desktop app](docs/desktop-app.md) | The Windows app for laptops: download, connecting, the tray icon, notifications, Start with Windows |
| [Discord bot](docs/discord.md) *(optional)* | Connecting a bot, all commands, reply visibility, notifications |
| [Commands and permissions](docs/commands.md) | Every command's default permission, changing permissions in the dashboard, logging |
| [Print queue](docs/print-queue.md) | Supported files, job states, starting prints safely, review and recovery |
| [Printer controls & camera](docs/printer-controls.md) | Temperatures, speed, fans, chamber, movement, lights, live view |
| [Swapmod A1m](docs/swapmod.md) | Plate-swap batches for equipped A1 minis |
| [AI failure detection](failureDetection/FAILURE_DETECTION.md) *(in release)* | Watching prints with an AI model (on the CPU or a Pi 5 AI HAT), pausing failed prints |
| [Team tools](docs/team-tools.md) | Notes, reminders, assignments, practice schedule, archiving, server and reboot |
| [Laptops (MeshCentral)](laptopManagement_Intergration/LAPTOPS.md) *(in release)* | Connecting MeshCentral and running approved laptop commands |
| [Updates & releases](docs/updates.md) | Dashboard and GitHub updates, release channels, manual updates, publishing releases |
| [Troubleshooting](docs/troubleshooting.md) | Logs, error IDs, diagnostic reports, problem reports, common problems |
| [Development](docs/development.md) | Code layout, running locally, tests, CI, building a release |
| [Web updater internals](Updater/WEB_UPDATES.md) *(in release)* | How the root-owned updater installs and rolls back releases |
| [Error catalog source](ERROR_SOURCES.md) *(in release)* | Where printer error descriptions come from |
| [Changelog](CHANGELOG.md) *(in release)* | What changed in each version |

## Safety

This software sends real commands to real machines. Keep these in mind:
- **Starts need confirmation.** A queued print only starts after someone confirms the plate is clear. An uncertain start is never retried automatically: the job becomes *needs review*.
- **"Submitted" isn't "done".** The printer can still reject a command. Check the printer or its telemetry.
- **Movement needs care.** Axis moves need a homed, idle printer and a clear path. The app can't see the printer, so it can't check this for you.
- **Not tested on physical hardware.** Features marked like this in the guides have only been tested against simulated printers. Try them on one printer first.

## Support

- Get a diagnostic report: dashboard **Settings → Diagnostics & error logs**, or `/diagnostics` in Discord.
- Send a problem report: dashboard **Settings → Send a problem report**, or `/reportissue` in Discord.

See [Troubleshooting](docs/troubleshooting.md) for details.

The printer error descriptions come from Bambu Studio resources. Their license is in `BAMBU_RESOURCE_LICENSE.txt`, and more detail is in [ERROR_SOURCES.md](ERROR_SOURCES.md).
