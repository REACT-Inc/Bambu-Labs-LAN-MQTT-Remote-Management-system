# Backup Discord bot

A second, team-only Discord bot that runs on a **separate Pi**. It takes over the **`/ftcteam`** commands automatically when the main 3D Printer Management Pi or its bot is offline, and hands them back when the main bot returns. It never runs printer commands.

## What it does

| Main Pi | Backup Pi |
|---|---|
| Answering, and its Discord bot connected | **Logged out.** It keeps a copy of the team data, refreshed every few seconds. |
| Not answering (stopped, crashed, frozen, powered off or off the network), or its Discord bot disconnected, for **90 s** | **Logs in** and registers `/ftcteam …` in your server(s), using its copy |
| Healthy again for **30 s** | **Logs out** and sends everything it changed back to the main Pi |

- **Team commands only:**
  - `/ftcteam attending`, `notattending`, `attendance`
  - `/ftcteam note save|find|delete`
  - `/ftcteam remind`, `reminders`, `cancelreminder`
  - `/ftcteam links`
  - `/ftcteam report assign`

  The permission rules (**Settings → Discord command permissions**) and the reply rules (public only in the commands channel) are copied from the main Pi, so they're the same as the main bot's.
- **No printer commands:**
  - no `/printer`, `/status`, queue or controls
  - no printer connections, cameras or updates

  Printers keep printing on their own while the main Pi is down.
- **Same data:** it serves the notes, reminders, attendance and report roster copied from the main Pi. While it's active it also **sends due reminders and practice report assignments**.
- **Nothing sent twice:** while the backup is active (or still has changes to send back), the main Pi holds its own reminders and report assignments. After a restart, the main Pi waits 20 s to hear from the backup first.
- **Changes go back:** new notes, deletions, attendance replies, reminders and assignments made on the backup are merged into the main Pi when it hands back. If the main Pi can't take them yet, the backup keeps them (also across a restart, in `backup-unmerged.json`) and retries every few seconds. If the main Pi rejects them as invalid, they're saved in `backup-rejected-<time>.json` in the backup's data folder instead, so the main Pi doesn't stay on hold.
- **Replies are marked** "Backup bot (team commands only)" at the bottom. The main Pi's Activity feed shows when the backup started and stopped.

## How it knows the main bot is online

- **Health check:** every 5 seconds the backup Pi calls `GET /backup-sync/state` on the main dashboard. The answer says whether the main Discord bot is connected, and carries the team data.
- **Frozen app:** the check is answered from the main app's event loop, so a frozen app (see #40) doesn't answer and counts as down.
- **Shared key:** both endpoints need the key from `backup_sync.key` (main Pi) / `backup_bot.sync_key` (backup Pi). Without `backup_sync` on the main Pi, the endpoints don't exist (404).

## Setup

1. **Create a second Discord application and bot** in the [Discord Developer Portal](https://discord.com/developers/applications). Use a name like *Printer Management (backup)*.
   - It must be a **different bot** from the main one. The same token can't answer for both.
   - Invite it to your server with the `bot` and `applications.commands` scopes. It needs **View Channel**, **Send Messages** and **Embed Links** in the channels your team uses.
2. **Connect the backup Pi to the main Pi's network.** Use **Tailscale** (recommended: the key and team data travel over plain HTTP, and Tailscale encrypts them) or the same LAN. The backup must reach the dashboard address the main Pi listens on, for example `http://100.101.102.103:8080`.
3. **Install on the backup Pi** from a release folder:
   ```bash
   sudo PM_MAIN_URL=http://100.101.102.103:8080 PM_BACKUP_TOKEN='SECOND-BOT-TOKEN' bash install-backup-pi.sh
   ```
   Without the variables it asks for them. It prints a new shared key, like:
   ```
   "backup_sync": {"key": "B57pxBL8GQCH7znoVWQzORkVTwC1PMY_Ejo56XF42GI"}
   ```
   To use your own key instead, pass `PM_SYNC_KEY=...` (at least 24 characters). Make one with:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
4. **Add the key on the main Pi.** Put that line in `/etc/3d-printer-management/config.json`, then restart with `sudo systemctl restart 3d-printer-management`.

The backup Pi's `/etc/3d-printer-management/config.json`:
```json
{
  "guild_ids": [],
  "admin_user_ids": [],
  "backup_bot": {
    "enabled": true,
    "token": "SECOND-BOT-TOKEN",
    "main_url": "http://100.101.102.103:8080",
    "sync_key": "SAME-AS-backup_sync.key-ON-THE-MAIN-PI",
    "failover_after": 90,
    "failback_after": 30
  }
}
```
- `guild_ids` and `admin_user_ids` are copied from the main Pi once it has been reached. Fill them in only if the backup might have to take over before it has ever reached the main Pi.
- `failover_after` (minimum 30 s) and `failback_after` (minimum 10 s) are optional.
- After editing, restart with `sudo systemctl restart pm-backup-bot`.

If anything is missing, the service logs what to fix and idles. Don't install the backup on the main Pi: the installer refuses, because the backup must keep running when the main Pi is down.

## Checking it

On the backup Pi:
```bash
systemctl status pm-backup-bot
sudo journalctl -u pm-backup-bot -n 50 --no-pager
```

- **The log shows the switches:** "starting the backup bot" when it takes over, "handing /ftcteam back" when the main bot returns, and "Sent the backup changes to the main Pi".
- **Wrong key or address:** "Main Pi answered 403" means the keys don't match; 404 means the main Pi has no `backup_sync` key yet. No answer at all looks like the main Pi being down, so the backup takes over. Check `main_url` with `curl -s -o /dev/null -w '%{http_code}\n' http://100.101.102.103:8080/health`.
- **To test:** on the main Pi, `sudo systemctl stop 3d-printer-management`. About 90 seconds later the backup comes online. Save a note with `/ftcteam note save`, then start the main service again. About 30 seconds after the main bot connects, the backup logs out and the note appears on the main Pi.

## Updating

Run the installer again from the new release folder on the backup Pi: `sudo bash install-backup-pi.sh`. It keeps the existing config. Keep the backup on the same version as the main Pi.

## Notes

- **Disconnects:** if Discord only drops the main bot for a moment, nothing happens. The backup waits the full `failover_after` time before taking over.
- **Network split:** if the main Pi is fine but the backup can't reach it (for example Tailscale is down on one of them), both bots answer `/ftcteam` and both may send reminders. Changes made on the backup are still merged when the link returns. For the same row, the backup's version wins.
- **Stale command list:** if the backup can't reach Discord while handing back, its commands stay in the list until it next starts. Using them then gives "The application did not respond", and the main bot's own `/ftcteam` commands keep working.
- **Team settings:** links, meeting days and the report roster are copied from the main Pi. Change them in the main dashboard; the backup has no dashboard.
