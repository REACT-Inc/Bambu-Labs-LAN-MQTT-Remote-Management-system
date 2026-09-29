# Backup Discord bot

A second, team-only Discord bot. It takes over the **`/ftcteam`** commands automatically when the main 3D Printer Management bot is offline, and hands them back when the main bot returns. It never runs printer commands.

## What it does

| Main service | Backup bot |
|---|---|
| Running, and its Discord bot connected | **Logged out.** It has no commands in Discord. |
| Stopped, crashed, frozen, or its Discord bot disconnected, for **90 s** | **Logs in** and registers `/ftcteam …` in your server(s) |
| Healthy again for **30 s** | Removes its commands and **logs out** |

- **Team commands only:**
  - `/ftcteam attending`, `notattending`, `attendance`
  - `/ftcteam note save|find|delete`
  - `/ftcteam remind`, `reminders`, `cancelreminder`
  - `/ftcteam links`
  - `/ftcteam report assign`

  The permission rules (**Settings → Discord command permissions**) and the reply rules (public only in the commands channel) are the same as the main bot's.
- **No printer commands:**
  - no `/printer`, `/status`, queue or controls
  - no printer connections, cameras or updates

  Printers keep printing on their own while the main service is down.
- **Same data:** it uses the same notes, reminders, attendance and report roster as the main service. While it's active it also **sends due reminders and practice report assignments**. It stops doing so before handing back, so nothing is sent twice.
- **Replies are marked** "Backup bot (team commands only)" at the bottom. The Activity feed records when the backup started and stopped.

## How it knows the main bot is online

- **Heartbeat file:** every 15 seconds, the main service writes `heartbeat.json` in its data folder with the time and whether its Discord bot is connected.
- **Frozen app:** the heartbeat is written from the main app's event loop, so a frozen app stops beating too (see #40).
- **Same machine:** the backup bot reads that file, so both services must run on the same machine. That's the default: the installer sets up both.

## Setup

1. **Create a second Discord application and bot** in the [Discord Developer Portal](https://discord.com/developers/applications). Use a name like *Printer Management (backup)*.
   - It must be a **different bot** from the main one. The same token can't answer for both.
   - Invite it to your server with the `bot` and `applications.commands` scopes. It needs **View Channel**, **Send Messages** and **Embed Links** in the channels your team uses.
2. **Add its token** to `/etc/3d-printer-management/config.json`:
   ```json
   "backup_bot": {
     "enabled": true,
     "token": "YOUR-SECOND-BOT-TOKEN",
     "failover_after": 90,
     "failback_after": 30
   }
   ```
   `failover_after` (minimum 30 s) and `failback_after` (minimum 10 s) are optional.
3. **Restart it:** `sudo systemctl restart pm-backup-bot`.

The service (`pm-backup-bot`) is installed by `install.sh` and `Updater/update.sh`. If your install was only ever updated from the dashboard, install it once with:
```bash
sudo bash Updater/install-backup-bot.sh
```

Without a token, or with `"enabled": false`, the service just idles.

## Checking it

```bash
systemctl status pm-backup-bot
sudo journalctl -u pm-backup-bot -n 50 --no-pager
cat /var/lib/3d-printer-management/heartbeat.json
```

- **The log shows the switches:** "starting the backup bot" when it takes over, and "handing /ftcteam back" when the main bot returns.
- **To test:** stop the main service with `sudo systemctl stop 3d-printer-management`, wait about 90 seconds, and the backup bot comes online. Start the main service again, and the backup logs out about 30 seconds after the main bot connects.

## Notes

- **Updates:** the backup restarts itself after an update, so it always runs the same version as the main service.
- **Disconnects:** if Discord only drops the main bot for a moment, nothing happens. The backup waits the full `failover_after` time before taking over.
- **Stale command list:** if the backup can't reach Discord while handing back, its commands stay in the list until it next starts. Using them then gives "The application did not respond", and the main bot's own `/ftcteam` commands keep working.
