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
| `model` | recommended | Set a model from the table below, e.g. `H2C`, `X1E`, `P2S`, `A2L`. Aliases such as `Bambu Lab X1 Carbon` and `A1 mini Combo` work. If omitted, the **name** is used to guess. Unknown models keep conservative control limits. |
| `camera_type` | for camera | `auto` selects the model's suggested LAN camera protocol, `rtsp` or `jpeg_tcp` overrides it. Omit or set `none` to disable camera features. RTSPS requires ffmpeg and LAN live view enabled on the printer; a matching model does not guarantee camera access on every firmware. |
| `error_model` | no | Forces which Bambu error catalog to use. Normally the first 3 characters of the serial pick it. |
| `ftp_tls_unwrap` | no | `true`/`false` overrides how FTPS uploads close their data connection. It's enabled automatically for the H2D and H2D Pro; for other H2 models test an upload and override this if necessary. See [Troubleshooting](troubleshooting.md#h2d-upload-fails-with-426). |

**Limits derived from `model`:**

| Configured model | Nozzle max | Bed max | Active chamber |
|---|---|---|---|
| H2S, H2D, H2D Pro, H2C | 350 °C | 120 °C | 40–65 °C (or off) |
| X2D | 300 °C | 120 °C | 40–65 °C (or off) |
| X1E | 320 °C | 110 °C | 40–60 °C (or off) |
| X1, X1C, P2S | 300 °C | 110 °C | — |
| P1P, P1S, A1 | 300 °C | 100 °C | — |
| A1 mini, A2L | 300 °C | 80 °C | — |
| Unknown | 300 °C | 80 °C | — |

Limits are conservative software bounds; some printers have regional power-dependent bed limits. The firmware may reject a command below these bounds. Set `model` explicitly for a printer whose display name is a nickname. `camera_type: "auto"` is optional, so existing installations with no camera setting remain unchanged. Newer P2/X2/H2 models use the fan controls reported by their air-duct telemetry; fans stay hidden until the printer reports them. Laser, cutting, multiple-tool selection and proprietary firmware features are outside the shared LAN control paths. Queue each file only after slicing it for that exact printer and check the first print on the device.

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
