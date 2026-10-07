# pm-doctor: the independent watchdog

`pm-doctor` keeps an eye on 3D Printer Management from the outside. It keeps working when the app won't start, crashes, or freezes while systemd still says "active (running)". You can always see what went wrong, and get told about it.

## What it does

Every 30 seconds it checks:

| Check | Counts as a problem when |
|---|---|
| The app service | it's `failed` or `inactive`, or restarting in a loop (3 restarts in 10 minutes) |
| The dashboard's `/health` | the service is "running" but `/health` doesn't answer within 2 s, for 2 minutes in a row (a frozen app) |
| Start-up errors | the journal shows a traceback or `Start-up failed` since the last start |
| Disk | less than 1 GB or 5% free on the system or data disk |
| Memory | less than 100 MB available |
| Temperature and power | CPU at 80 °C or more; a Raspberry Pi reporting under-voltage or throttling now |
| Network | shown, not alerted: whether each printer's MQTT port 8883, Discord, GitHub and Tailscale are reachable |

When a problem appears, the doctor opens an **incident**:

1. **Record:** it saves `/var/lib/pm-doctor/incidents/<id>/`, with secrets removed. The record holds the checks, the last 300 journal lines for the app and the updater, and the tail of `management.log`. It keeps the last 30 incidents.
2. **Stack dump for a frozen app:** it asks the app to write every thread's stack to its log (`SIGUSR1`), so the record shows the exact line that is stuck.
3. **Alert:** it sends one message when the incident opens and one when it clears, never one every 30 seconds:
   - **Discord webhook:** a plain web request, so it works while the bot is down;
   - **GitHub issue:** optional, with the checks attached.
4. **Restart (optional, off by default):** after the set number of minutes without an answer, it restarts the app, at most every 10 minutes, and says so in the alert.

## Settings

`/etc/pm-doctor/config.json` (root only, created on install):

```json
{"discord_webhook": "", "github": {"repository": "", "token": ""}, "restart_after_minutes": 0,
 "status_page": true, "port": 8081, "interval": 30}
```

| Setting | Meaning |
|---|---|
| `discord_webhook` | A Discord channel webhook URL (channel settings → Integrations → Webhooks) |
| `github` | `repository` (`owner/name`) and a token with Issues: read and write, to open an issue per incident |
| `restart_after_minutes` | Restart a frozen app after this many minutes; `0` = never |
| `status_page` / `port` | The status page and its port |
| `interval` | Seconds between checks (at least 10) |

After changing it: `sudo systemctl restart pm-doctor`.

## Status page

Open `http://<pi address>:8081/`. The **Diagnostics** panel in the dashboard links to it. Sign in with the **dashboard password**: the page checks it against the dashboard's own password hash, so it works while the dashboard is down. It shows:
- the app's state and its last start-up error;
- current problems and the open incident;
- Pi health and network reachability;
- **Restart the app**;
- downloads: each incident as a ZIP, or a fresh report.

It listens on the same addresses as the dashboard (`listen` in `config.json`) and on `127.0.0.1`.

## Command line

```bash
sudo pm-doctor            # check now and print the results
sudo pm-doctor report     # write an incident ZIP (secrets removed) and print its path
```

Useful over SSH when nothing else works.

## Independence

- **Standard library only:** it never imports the app, so a bad release, a broken virtualenv or a broken config can't stop it.
- **Separate install:** it lives in `/usr/local/lib/pm-doctor`, with its own systemd unit (`pm-doctor.service`, `Restart=always`).
- **Light:** CPU is capped at 5% and memory at 64 MB.
- **Locked down:** it runs as root only so it can read the journal, signal the app and (when enabled) restart it. The system is read-only to it except `/var/lib/pm-doctor`. It never sends anything to the printers.
- **Updates:**
  - `install.sh` and `Updater/update.sh` install or update it, but only when its own version (`DOCTOR_VERSION`) changes. `PM_DOCTOR=0` skips it.
  - Dashboard and GitHub updates never install it, because they don't run scripts from a ZIP. Run `sudo bash Updater/install-doctor.sh` from an extracted release to install or update it by hand.
- **Removing it:** `sudo systemctl disable --now pm-doctor && sudo rm -r /usr/local/lib/pm-doctor /usr/local/sbin/pm-doctor /etc/systemd/system/pm-doctor.service`.
