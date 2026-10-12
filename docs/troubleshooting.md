# Troubleshooting

- [Where to look first](#where-to-look-first)
- [Error IDs](#error-ids)
- [Diagnostic reports](#diagnostic-reports)
- [Sending a problem report](#sending-a-problem-report)
- [Common problems](#common-problems)

## Where to look first

| What | Where |
|---|---|
| Is the service running? | `sudo systemctl status 3d-printer-management --no-pager` |
| Service log | `sudo tail -n 100 /var/lib/3d-printer-management/logs/management.log`, or `sudo journalctl -u 3d-printer-management -n 100 --no-pager` |
| Recent errors | Dashboard **Settings → Diagnostics & error logs** |
| What happened to printers and the queue | Dashboard **Activity** |
| Update problems | `sudo journalctl -u pm-web-update -n 60 --no-pager`. See [WEB_UPDATES.md](../Updater/WEB_UPDATES.md). |

About the log file:
- **What's in it:** warnings, errors, and crashes in background tasks, which older versions lost.
- **Rotation:** it rotates at 2 MB and keeps 5 older files.
- **Secrets:** passwords, tokens and printer access codes are removed before anything is written.

## Error IDs

When a Discord command, a button or a dashboard action fails, the user sees a short **error ID** such as `3f9a1c2e`. The same ID is written to the log next to the full error:

```bash
sudo grep 3f9a1c2e /var/lib/3d-printer-management/logs/management.log*
```

When reporting a problem, always include the error ID.

## Diagnostic reports

A ZIP with everything needed to investigate a problem:

| File | Contents |
|---|---|
| `summary.json` | Version, Python, OS, uptime, Discord connection |
| `printers.json` | Each printer's connection, state, errors, HMS alerts and latest telemetry |
| `config.json`, `settings.json` | Configuration, with secrets replaced by `[redacted]` and serial numbers shortened |
| `events.json` | Recent Activity |
| `recent_errors.json` | The last 100 errors, with IDs |
| `logs/` | Up to ~6 MB of recent log files |

How to get one:
- **Dashboard:** **Settings → Diagnostics & error logs → Download diagnostic report**.
- **Discord:** `/diagnostics` (admins). The report is sent privately.

The report still contains **printer names and IP addresses**, so share it only with people you trust.

## Sending a problem report

Users can send a report straight to the developers as a **GitHub issue**:
- **Dashboard:** **Settings → Send a problem report**.
- **Discord:** `/reportissue` (admins).

The issue contains:
- the title and description typed in
- the version and system details
- a printer status table
- the last 10 errors with their IDs
- the last 80 log lines

Because issues may be public, **IP addresses, serial numbers, passwords, tokens and access codes are removed**. One report can be sent every 2 minutes.

**Setting it up (once):**
1. **Create a token:** create a **fine-grained GitHub token** with **Issues: Read and write** on the repository.
2. **Save it:** enter it under **Settings → Send a problem report → Report destination**, which is also where the repository can be changed. It's stored in `issue-reports.json` (mode 600) and never shown again.
3. **Or preset it for every install** in `config.json`:
   ```json
   "issue_reports": {"destination": "github", "repository": "REACT-Inc/Bambu-Labs-LAN-MQTT-Remote-Management-system", "token": "github_pat_..."}
   ```
   Dashboard settings take priority.

Other destinations, such as a Discord webhook or email, can be added in `issue_reports.py` by registering a sender in `SENDERS`.

## Common problems

### The dashboard doesn't load

- **Is the service running?** Check the status. If it keeps restarting, read the log: a JSON error in `config.json` is the usual cause.
- **Is the address right?** Check you're using an address from `listen` in `config.json`, and that your device can reach it. For Tailscale, the device must be on the tailnet and allowed by your policy; for a LAN address, the same network.
- **Only on the Pi?** `http://localhost:8080` only works on the Pi itself.

### A printer shows OFFLINE

- **Check the printer settings:** in `config.json`, `ip`, `serial` and `access_code` must match the printer's screen. Give printers a fixed address in your router.
- **Check the network:** the Pi must reach the printer on port **8883** (MQTT over TLS). Check LAN mode and developer mode on the printer.
- **Look at the log:** search it for the printer's name. Connection errors and retries are logged.

### Camera shows "Unavailable"

- **Check the camera setting:** `camera_type` must be set (`jpeg_tcp` for A1-family, `rtsp` for H2D). RTSP needs `ffmpeg` installed.
- **Close other camera clients:** Bambu Studio and Handy count as clients, and the printer may accept only one.
- **Look at the log:**
  ```bash
  sudo journalctl -u 3d-printer-management --since '5 minutes ago' --no-pager | grep -iE 'camera|snapshot'
  ```

### H2D upload fails with 426

`426` (or `error_temp: 426`) is an **FTP transfer error**, not a temperature.

**How the app handles it:**
- **Longer timeout:** H2D uploads close the encrypted data connection in a special way, and allow 60 seconds per FTP step.
- **Strict checks:** an upload only counts if the printer confirms completion (`226`) **and** the remote file size matches.
- **No retries:** a 426 always fails the upload, and printing is never retried automatically.
- **Error detail:** the queue error shows the step and the bytes sent.
- **Override:** you can set `ftp_tls_unwrap: true` or `false` on the printer entry to change the behaviour.

**If it keeps happening:**
1. Check the H2D recognises its USB drive and reports free space. Don't reformat a drive that holds files you need.
2. Send a problem report with the queue error, or attach the output of `sudo journalctl -u 3d-printer-management -n 100 --no-pager`.

### The service restarted itself ("Event loop stalled")

The dashboard and the Discord bot share one event loop. A watchdog thread checks it every second:
- **After 15 seconds without progress:** it logs `Event loop has not run for … seconds` and writes every thread's stack to the service journal and to `logs/stalls.log`.
- **After 60 seconds:** it exits, and systemd restarts the service about 10 seconds later.
- **Jobs that were running:** they become **needs review** after the restart. They're never resubmitted.

**Where to find the evidence:** the stack dump shows where it was stuck.
- `sudo journalctl -u 3d-printer-management -n 200 --no-pager`
- `/var/lib/3d-printer-management/logs/stalls.log`
- the **diagnostic report** ZIP, which includes `stalls.log`

Attach it to a problem report or GitHub issue. If this repeats, it's a bug; please report it with that file.

### A job is stuck in "needs review"

This is deliberate: the app couldn't confirm what the printer did. Look at the printer, then record what actually happened with **Mark finished / failed / cancelled**. See [Print queue](print-queue.md#when-a-job-needs-review).

### A Discord command doesn't appear or is refused

- **Bot offline after an internet outage:** the dashboard stays available while the bot reconnects. Escaped connection failures retry after 5, 10, 20, 40, then at most 60 seconds. An invalid token or missing required permissions needs an administrator to correct the configuration; check `sudo journalctl -u 3d-printer-management -n 100 --no-pager`.
- **New commands** can take a minute to sync after the bot connects.
- **"Use this bot in an authorized server":** the server's ID isn't in `guild_ids`.
- **"Administrators and approved user IDs only":** add the user under **Settings → Approved user IDs**.
- **Command hidden or blocked:** check **Server Settings → Integrations** in Discord. Server-side overrides can hide or block commands.

### A printer rejected a setting

Rejections appear in Activity as **Control rejected**, with the printer's reason and code. For example:
- **Chamber heating:** refused while PLA, PETG or TPU is loaded (code `-2`).
- **Fans in automatic mode:** they can't be set by hand. Change the airflow mode on the printer.
