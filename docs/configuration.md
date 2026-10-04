# Configuration

All settings live in two places:

- **`/etc/3d-printer-management/config.json`:** the private configuration. You edit this by hand, then restart the service. It holds the Discord token, servers, printers, network and features.
- **The dashboard's Settings page** (stored in `/var/lib/3d-printer-management/`): channel IDs, approved users, printer display names, team settings, GitHub and problem-report destinations. These apply immediately, with no restart.

- [Editing config.json](#editing-configjson)
- [config.json reference](#configjson-reference)
- [Printer entries](#printer-entries)
- [Files and folders](#files-and-folders)
- [Service commands](#service-commands)
- [Resetting a lost dashboard password](#resetting-a-lost-dashboard-password)

## Editing config.json

```bash
sudo nano /etc/3d-printer-management/config.json
sudo systemctl restart 3d-printer-management
```

The file is strict JSON:
- quoted keys
- lowercase `true` / `false`
- no comments
- no trailing commas

A syntax error stops the service from starting. Check with:

```bash
sudo python3 -m json.tool /etc/3d-printer-management/config.json >/dev/null && echo valid
```

## config.json reference

| Key | Type | Default | Meaning |
|---|---|---|---|
| `discord_token` | string | `""` | Discord bot token. Empty = dashboard-only mode (no Discord). |
| `guild_ids` | list of numbers | `[]` | Discord servers the bot answers in. Commands anywhere else are refused. |
| `admin_user_ids` | list of numbers | `[]` | Discord user IDs allowed to run admin commands without being server administrators. Overridden by **Settings → Approved user IDs** once saved there. |
| `printers` | list | `[]` | Printers to manage. See [Printer entries](#printer-entries). |
| `demo` | boolean | `false` | `true` = don't connect to printers; simulate them. Demo mode is also on when `printers` is empty. |
| `example_data` | object | two demo printers | Demo mode only: fake telemetry per printer name, in the same shape as a Bambu MQTT `print` report. |
| `listen` | list of IPs | `["127.0.0.1", "<chosen IP>"]` | Addresses the dashboard listens on. Set by the installer. |
| `port` | number | `8080` | Dashboard port |
| `allow_host_reboot` | boolean | `false` | Allow `/reboot` and **Server → Reboot Pi…**. Set with `install.sh --enable-reboot`. See [Installation](installation.md#optional-allow-rebooting-the-pi-from-discord-or-the-dashboard). |
| `failure_detection` | object | off | AI print-failure detection with a Raspberry Pi 5 AI HAT: `{"enabled": true, "model": "/path/model.hef", "action": "notify"}`. See [AI failure detection](../failureDetection/FAILURE_DETECTION.md). |
| `issue_reports` | object | GitHub, this repo | Default destination for problem reports: `{"destination": "github", "repository": "owner/name", "token": "..."}`. Dashboard settings override it. See [Troubleshooting](troubleshooting.md#sending-a-problem-report). |

Example:

```json
{
  "discord_token": "MTIz...",
  "guild_ids": [123456789012345678],
  "admin_user_ids": [234567890123456789],
  "demo": false,
  "listen": ["127.0.0.1", "100.101.102.103"],
  "port": 8080,
  "printers": [
    {"name": "Andrew", "model": "H2D", "ip": "192.168.1.40", "serial": "094XXXXXXXXXXXX", "access_code": "12345678", "camera_type": "rtsp"},
    {"name": "BOB", "model": "A1 mini", "ip": "192.168.1.41", "serial": "030XXXXXXXXXXXX", "access_code": "87654321", "camera_type": "jpeg_tcp"}
  ]
}
```

## Printer entries

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Internal name, unique (case-insensitive). Used for queues, history and Discord. Use **Rename** to change the *display* name without affecting history. |
| `ip` | live | Printer's LAN IP address. Give it a fixed/reserved address in your router. |
| `serial` | live | Printer serial number (MQTT topic and error-model detection). |
| `access_code` | live | LAN access code from the printer's screen. |
| `model` | recommended | E.g. `H2D`, `A1 mini`, `P1S`, `X1 Carbon`. Sets temperature limits, chamber support and fan layout (see [Supported printers](#supported-printers)). If omitted, the serial number identifies the model, and failing that the **name**. |
| `camera_type` | for camera | `rtsp` (H2-series, X1-series, P2S, X2D; needs ffmpeg) or `jpeg_tcp` (A1-series and P1-series). See the table below. Omit to disable camera features for that printer. |
| `limits` | no | Override the nozzle / bed maximums, e.g. `{"nozzle": 300, "bed": 100}` (up to 400 / 150 °C). Useful for a model the app doesn't know, or if a default is wrong for your printer. |
| `error_model` | no | Forces which Bambu error catalog to use. Normally the first 3 characters of the serial pick it. |
| `ftp_tls_unwrap` | no | `true`/`false` overrides how FTPS uploads close their data connection. It's enabled automatically for H2-series printers. See [Troubleshooting](troubleshooting.md#h2d-upload-fails-with-426). |

## Supported printers

The app uses each printer's model for its temperature limits, chamber heating, fans and FTPS settings (`printer_models.py`). The values come from Bambu Studio's printer definitions; bed maximums Studio doesn't list come from Bambu's published specs.

| Model | Tested | `camera_type` | Nozzle max | Bed max | Chamber heating | Serial starts with |
|---|---|---|---|---|---|---|
| A1 mini | ✅ | `jpeg_tcp` | 300 °C | 80 °C | — | `030` |
| A1 | ✅ | `jpeg_tcp` | 300 °C | 100 °C | — | `039` |
| A2L | untested | `jpeg_tcp` | 300 °C | 100 °C | — | `26A` |
| P1P | untested | `jpeg_tcp` | 300 °C | 100 °C | — | `01S` |
| P1S | untested | `jpeg_tcp` | 300 °C | 100 °C | — | `01P` |
| P2S | untested | `rtsp` | 300 °C | 110 °C | — | `22E` |
| X1 | untested | `rtsp` | 300 °C | 110 °C | — | `00W` |
| X1 Carbon | untested | `rtsp` | 300 °C | 110 °C | — | `00M` |
| X1E | untested | `rtsp` | 320 °C | 120 °C | not offered¹ | `03W` |
| X2D | untested | `rtsp` | 300 °C | 120 °C | 40–65 °C | `20P` |
| H2D | ✅ | `rtsp` | 350 °C | 120 °C | 40–65 °C | `094` |
| H2D Pro | untested | `rtsp` | 350 °C | 120 °C | 40–65 °C | `239` |
| H2S | untested | `rtsp` | 350 °C | 120 °C | 40–65 °C | `093` |
| H2C | untested | `rtsp` | 350 °C | 120 °C | 40–65 °C | `31B` |
| Not recognised | — | — | 300 °C | 80 °C | — | |

¹ The X1E heats its chamber, but not necessarily with the command the app sends to the H2D, so the app doesn't offer it. Set the chamber on the printer.

- **Untested** means nobody has run the app against that printer yet. Monitoring, the queue and controls use the same LAN protocol, so they should work. Report anything that doesn't with **Report a problem** in the dashboard.
- **Dual-nozzle printers** (H2D, H2D Pro, H2C, X2D) show left/right nozzle details where the printer reports them.
- **Fans:** printers that report their airflow layout (newer firmware) show exactly the fans they report. Older firmware falls back to the model's known fans.

## Files and folders

| Path | Contents |
|---|---|
| `/etc/3d-printer-management/config.json` | Private configuration (see above). Contains secrets: keep mode 640, group `printermanager`. |
| `/opt/3d-printer-management/` | Application code and `venv/`. After a dashboard/GitHub update this is a link into `/opt/3d-printer-management-releases/`. |
| `/var/lib/3d-printer-management/` | All data (owned by `printermanager`): |
| ├ `management.sqlite3` | Queue jobs, history, activity feed, Swapmod state (SQLite WAL; stop the service before copying it) |
| ├ `uploads/` | Uploaded `.3mf` files. Not cleaned automatically. |
| ├ `settings.json` | Channel IDs (`notification_channel_id`, and `commands_channel_id`: the only channel where Discord replies are public), approved user IDs, printer display names |
| ├ `auth.json` | Dashboard password hash (scrypt) |
| ├ `team.json` | Team links, practice schedule and roster |
| ├ `meshcentral.json` | MeshCentral URL, token and allowed commands. See [LAPTOPS.md](../laptopManagement_Intergration/LAPTOPS.md). |
| ├ `github-updates.json` | GitHub release settings and token (mode 600) |
| ├ `issue-reports.json` | Problem-report destination and token (mode 600) |
| ├ `logs/management.log` | Service log (rotates at 2 MB, 5 old files kept) |
| └ `updates/` | Update ZIPs waiting for the updater |
| `/var/lib/pm-updater/` | Updater status, journal and root-only backups |
| `/var/backups/3d-printer-management/` | Backups made by `install.sh` |

**Backups:** to back up everything important, stop the service and copy `/etc/3d-printer-management/` and `/var/lib/3d-printer-management/`.

## Service commands

```bash
sudo systemctl status 3d-printer-management --no-pager     # is it running?
sudo systemctl restart 3d-printer-management               # apply config.json changes
sudo journalctl -u 3d-printer-management -n 80 --no-pager  # recent log (also in logs/management.log)
sudo journalctl -u 3d-printer-management -f                # follow the log live
```

**Sessions:**
- Dashboard sessions last 12 hours.
- Restarting the service signs everyone out.
- Changing the dashboard password signs out every session.

## Resetting a lost dashboard password

```bash
sudo systemctl stop 3d-printer-management
sudo mv /var/lib/3d-printer-management/auth.json /var/lib/3d-printer-management/auth.json.backup
sudo /opt/3d-printer-management/venv/bin/python /opt/3d-printer-management/configure.py
sudo chown printermanager:printermanager /var/lib/3d-printer-management/auth.json
sudo systemctl start 3d-printer-management
```

`configure.py` prints a new random password and leaves the rest of the configuration untouched.
