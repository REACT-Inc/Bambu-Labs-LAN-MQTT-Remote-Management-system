# 3D Printer Management

A Raspberry Pi 4/5 application with a local web dashboard and a Discord interface sharing the same printer connections, job queues, files, and history. This is a self-hosted Python application, not a cloud-hosted website.

## Updates from the dashboard

Install this release once with `sudo bash update.sh` from the extracted folder. Then use **Settings \& help → Software update** to upload and install future management ZIPs. Config, queues and print files are preserved. Active prints block updates; failed dashboard startup triggers rollback. See [WEB\_UPDATES.md](WEB_UPDATES.md) for recovery and release-building details.

### Release channels (stable, beta, alpha)

Under **Settings → GitHub releases**, choose which releases this Pi follows:

| Channel | Receives | Tags |
|---|---|---|
| **Stable** (default) | Tested releases only | `v1.2.0` |
| **Beta** | Beta pre-releases and stable releases | `v1.2.0-beta.1` |
| **Alpha** | Alpha and beta pre-releases and stable releases | `v1.2.0-alpha.1` |

The Pi installs the newest release on its channel, where alpha < beta < stable for the same version (`1.2.0-alpha.3` < `1.2.0-beta.1` < `1.2.0`). With **Automatically install newer releases** turned on, new releases on the channel install when all printers are idle. Otherwise, use **Check now** and **Install release…**.

Moving to a more stable channel never downgrades. For example, a Pi on `1.2.0-beta.2` that switches to Stable stays on `1.2.0-beta.2` until `1.2.0` (or newer) is released.

**Publishing:** run the **Publish release** workflow with the new tag:
* **Stable** tags (`v1.2.0`) must run from `main` and become GitHub's "Latest" release.
* **Beta and alpha** tags (`v1.2.0-beta.1`, `v1.2.0-alpha.1`) can run from any branch, such as `beta-v1.1`. They are published as GitHub pre-releases, so stable installs never see them.

Never reuse a tag.

## Install on the existing Pi

1. Download and extract `3d-printer-management.zip` on the Pi.
2. Ensure Tailscale is connected, then run:

```bash
   cd printer-management
   sudo bash install.sh
   ```

3. Save the initial dashboard password printed by the installer.
4. Open the printed `http://100.x.x.x:8080` address from another device on your tailnet. That device must be allowed to reach the Pi on TCP port 8080 by your Tailscale policy and any host firewall.
5. Sign in and change the password in **Settings \& help**.

The installer reads literal settings from `/tmp/printer\_discord\_bot.py`, including the existing Discord token, allowed server IDs, printers, and approved user IDs. It copies channel settings into permanent storage, backs up the old source, and replaces `printer-discord-bot` with the `3d-printer-management` service after checking the dashboard health. Do not run both bot services simultaneously.

It binds to loopback and the Pi's Tailscale IPv4 address, rather than every network interface. The Pi's `localhost` URL works only on the Pi; other computers use its Tailscale IP. You can explicitly choose a LAN address with `sudo PM\_LISTEN\_IP=192.168.1.50 bash install.sh`. Tailscale installation, sign-in, firewall rules, and tailnet access changes are not performed by the installer.

For an existing bot at a different path: `sudo bash install.sh --old /path/to/printer\_discord\_bot.py`.
For a fresh demo without a previous bot: `sudo bash install.sh --demo` (Tailscale or PM\_LISTEN\_IP is still required).

## What works in both interfaces

* Printer connection and print state, percentage, layers, temperatures, and last telemetry.
* Camera snapshots when the model/firmware and camera library permit them.
* AMS and external spool information.
* Pause, resume, and confirmed stop.
* Upload a sliced `.3mf`, or queue an existing file by its exact path on the printer.
* Per-printer queues, manual start, reorder, removal, and history.
* Requeue a previously completed managed print.
* Notification-channel and public-command-channel settings.
* Approved Discord user IDs alongside the server Administrator permission.

The web dashboard uses a shared administrator password. Anyone with this password has management access. In Discord, every member of an allowed server can view printers, queue jobs, start and manage the queue, pause/resume/stop, reprint, switch lights, and set fans and print speed. Each of those actions asks for confirmation and is logged with who ran it. Temperatures, chamber heating, axis moves, Discord settings, Swapmod, DMs, channel archiving, laptops and the Pi require a server administrator or approved user ID. These are the defaults: administrators can change who may use each command (Everyone, allowed roles, admins only, or off) in the dashboard under **Settings → Discord command permissions**. See [COMMANDS.md](COMMANDS.md) for the full list. Neither knowing the IP address nor being on Tailscale bypasses the dashboard login.

## Discord commands

The full list of commands, their options, and who can run each one (Everyone or Admin) is in [COMMANDS.md](COMMANDS.md), along with the Discord permissions the bot needs.

* `/status`, `/printer`, `/filaments`, `/pause`, `/resume`, `/stop`, `/help`
* `/queueadd name:BOB file:<sliced .3mf>` or `/queueadd name:BOB remote:cache/model.gcode.3mf`
* `/queue name:BOB`
* `/queuestart name:BOB` (confirms plate clearance and starts the first waiting job)
* `/queuemanage job\_id:<ID> action:up|down|remove|resolve\_finished|resolve\_failed|resolve\_cancelled`
* `/reprint name:BOB` (confirms adding the most recent completed managed job back to the queue)
* `/setnotificationchannel`, `/setcommandschannel`

Partial names and typos prompt confirmation; ambiguous or omitted names show printer buttons. Buttons belong to the initiating user and expire after 60 seconds. Existing Discord integration overrides can still hide or block commands; check Server Settings → Integrations if an approved user cannot see one.

The current reprint workflow deliberately uses recorded queue files/options. It does not guess an SD-card path from the printer's human-readable last-job name.

## Queue and file behavior

Use a Bambu Studio or OrcaSlicer **sliced .3mf containing `Metadata/plate\_N.gcode`**, prepared for the actual printer/nozzle/material. The app does not slice STL files or convert unsliced projects. It checks the selected plate exists in an uploaded archive, but cannot certify printer/material compatibility; the start confirmation requires the operator to check it.

Uploads are saved on the Pi, then transferred over implicit FTPS to the selected printer before MQTT submission. Files already on the printer are entered by relative path, for example `cache/part.gcode.3mf`; their existence/plate must be correct. Existing printer files are not browsed automatically in this release. Select a plate, bed type, external spool or AMS, and AMS mapping for each job. Mapping `0,1` maps the first two sliced materials to those tray indices. H2D-specific advanced features such as tool switching are not modeled by this generic queue interface.

The web upload limit is 256 MiB. Discord may have a smaller attachment limit. Uploaded files and historical records remain on the Pi until you clean them up; there is no automatic retention policy in this version. Unreferenced uploads from cancelled/invalid forms can be removed manually after checking the queue database.

**Never automatically starts the next job.** Clear the plate, check material and file compatibility, then use **Start next** or `/queuestart`. Jobs enter `staging`, then `awaiting\_start`. A matching running report is required before they become `printing`. A completed/failed report for another file does not resolve the queue job. Upload or publish success alone never means the print successfully started.

If submission cannot be confirmed within two minutes, the job becomes `needs\_review`. Active jobs also become `needs\_review` after an application restart. Inspect the physical printer and record its outcome before another queued job can start. The application never automatically resubmits an uncertain start. Stop during staging cancels pending submission; after a print command may have been sent, stopping requires manual outcome review.

Starting a print from Bambu Studio or the printer panel remains outside this app's locking. Avoid simultaneous external starts while staging a queue job. The service checks fresh idle telemetry before staging and again immediately before submitting, but cannot make an atomic reservation across independent external controllers.

## Live printer requirements and current limits

The Pi needs network reachability to each printer: MQTT/TLS port 8883, FTPS port 990 plus its data connection, and camera access when configured. Bambu firmware LAN/developer-mode and third-party-control restrictions can affect availability. Existing access codes and serials are imported. Encrypted printer connections use the same local self-signed-certificate acceptance model as the original bot; keep the Pi-to-printer network trusted.

The live transfer/start path is implemented using BambuTools' FTPS implementation and its documented MQTT command shape. It has **not been tested against your physical H2D/A1/A1 Mini fleet**. Test one small sliced file on one cleared printer first. If the printer's firmware reports different filename identifiers, the app will require manual review instead of claiming a successful start.

The dashboard refreshes every five seconds; it is not a live video stream. Camera snapshots can be unavailable while another capture is running. Legacy notification styles, MQTT reconnects, and example data support are retained. Three-hour notification simulations from the earlier standalone script are separate from these queues and do not update this service's state.

## Files and service commands

* App: `/opt/3d-printer-management`
* Private config: `/etc/3d-printer-management/config.json`
* Persistent data: `/var/lib/3d-printer-management`
* Queue database: `management.sqlite3` (SQLite WAL; stop the service before a simple filesystem backup)
* Uploaded files: `uploads/`
* Channel settings: `settings.json`
* Hashed dashboard password: `auth.json`

```bash
sudo systemctl status 3d-printer-management --no-pager
sudo journalctl -u 3d-printer-management -n 80 --no-pager
sudo systemctl restart 3d-printer-management
sudo nano /etc/3d-printer-management/config.json
```

Config uses JSON: lowercase `true`/`false`, quoted keys, no comments. Change printer credentials/addresses in that file and restart. `demo: true` disables real printer connections and runs short fake queue jobs (roughly 22 seconds). Demo pause/resume/stop affect the simulated job. Demo jobs are tagged and cannot be started after switching to live mode; remove old demo queue entries and submit new live jobs. Set `demo` to `false` only after checking the imported printer configuration. If the original bot had a temporary `PRINTERS=\[]` override, import preserves earlier real printer entries but enables demo mode.

Settings-page changes persist without restarting. A dashboard password change invalidates all web sessions. Sessions also expire after 12 hours or a restart.

To reset a lost dashboard password:

```bash
sudo systemctl stop 3d-printer-management
sudo mv /var/lib/3d-printer-management/auth.json /var/lib/3d-printer-management/auth.json.backup
sudo /opt/3d-printer-management/venv/bin/python /opt/3d-printer-management/configure.py
sudo chown printermanager:printermanager /var/lib/3d-printer-management/auth.json
sudo systemctl start 3d-printer-management
```

The command prints a newly generated password; it preserves the existing configuration.

To return to the original bot while its `/tmp` source still exists:

```bash
sudo systemctl disable --now 3d-printer-management
sudo systemctl enable --now printer-discord-bot
```

The installer retains a backup under `/var/backups/3d-printer-management/`. The previous `/tmp` source may disappear on reboot; use that backup if restoring later. No bot token or printer access code is included in this download.

## Validation and implementation notes

Includes automated tests for SQLite queue persistence/FIFO, concurrent starts, restart recovery, matching telemetry, stale data, stop-during-staging behavior, file validation, session authentication, CSRF/origin checks, and Discord ID precision.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Sources used for the Bambu adapter and Tailscale access:

* https://bambutools.github.io/bambulabs\_api/api/printer.html
* https://github.com/BambuTools/bambulabs\_api
* https://tailscale.com/docs/how-to/connect-to-devices

BambuTools is a community-maintained integration, not a guarantee of firmware compatibility. Dependencies are installed from PyPI on the Pi. No package installation, Tailscale change, or service deployment has been performed on your Pi by this chat.

## Team tools added from the requested feature list

The **Team tools**, **Laptops**, and **Server** dashboard tabs share their records with Discord:

* `/ftc`, `/website`, `/management`: links configured in Team settings. No invite or website URL is guessed.
* `/rememberthis title text`, `/remember query`, `/forget note\_id`: shared server-scoped team notes. Owners can delete their own notes; admins can delete any. The authenticated web administrator can view/manage all allowed-server records.
* `/remindme minutes text`, `/reminders`, `/cancelreminder`: persistent personal DM reminders. Web admins can schedule a DM reminder for an explicit Discord user ID. Recipients must enable DMs. Failed deliveries are recorded instead of silently falling back to a public channel. The shared dashboard administrator can view stored reminder records.
* `/assign report`: randomly choose a report writer from the configured roster and post an assignment. The least-used members are chosen first, so everyone gets turns before repeats.
* Scheduled practices: configure time zone, weekdays (0=Monday), local time, eligible user IDs, a practice text channel, and optionally a team role to ping. Enable automatic announcements after saving the configuration. One assignment is stored per scheduled occurrence. There is a 20-minute catch-up window; older missed practices are skipped. Pause the schedule for holidays. The bot needs access to the practice channel and permission to mention the configured role if that role is not mentionable.
* `/archive channel`, `/unarchive channel`: confirmed text-channel archiving. Original category and permission overwrites are saved, no messages are deleted. Existing views are preserved and non-admin sends/thread creation are denied; Discord Administrators can still write. Configured bot/practice channels are protected. The bot needs Manage Channels and sufficient permission-management rights. Failed/interrupted archives keep the original snapshot so you can restore; do not manually overwrite the saved archive record. Deleted roles/members cannot be restored.
* `/laptops`, `/laptop cmd`, `/laptop result`: MeshCentral laptop status, configured command names, and returned results. Uses existing MeshCentral agents; no separate Python laptop agent. Configure the HTTPS server URL and account login token in the dashboard Laptops tab. See `LAPTOPS.md`.
* `/server`: Pi hostname, uptime, disk and temperature.
* `/reboot`: confirmed management-Pi reboot. Disabled until locally enabled. It is not a printer reboot command.

To opt in to the Pi reboot command, rerun the installer with:

```bash
sudo bash install.sh --enable-reboot
```

This grants the dedicated service account one root-owned, no-argument reboot helper through sudoers. Only approved Discord admins or authenticated dashboard managers can trigger it through the app. Reboot is refused while managed queue jobs are staging, awaiting start, printing, or paused. The action interrupts this Pi's other services too. To disable it, set `allow\_host\_reboot` to `false` in the private config, remove `/etc/sudoers.d/printermanager-reboot`, and restart the service.

Practice assignments and reminders are delivered by this Pi service, not ChatGPT automations. Keep the Pi and its Discord connection running. A crash during delivery is marked for review, not automatically resent, because the service cannot know whether Discord accepted the prior message. No live Discord messages, enrollments, channel archives, or reboots have been executed while building this package.



## Temperature, speed, fan and movement controls

Open **Details \& camera** on a printer card. The live camera and printer controls
are in that dialog. Controls use the existing dashboard password. Discord:

* `/temperature target:bed degrees:60 name:Andrew`
* `/temperature target:nozzle degrees:210 name:Andrew`
* `/speed mode:standard name:Andrew`
* `/fan percent:100 name:Andrew` (part cooling fan)
* `/move axis:X millimeters:1 name:Andrew`
* `/move axis:Z millimeters:-1 name:Andrew`

`/temperature`, `/chamber` and `/move` require a server administrator or approved user ID; `/speed`, `/fan` and `/fanall` are open to every member. Responses
and confirmations are visible in the server channel. Partial names use the existing printer picker.
Commands require confirmation and are revalidated when confirmed.

Movement uses relative printer coordinates: X/Y up to ±10 mm; Z up to ±1 mm;
minimum 0.1 mm, at most one request per three seconds. It requires IDLE/FINISH,
no print error, a connected printer, telemetry less than 15 seconds old, and no
active/unresolved management job. Home using the printer's controls first, verify
clearance, and watch it move. The application cannot verify physical clearance or
homing from current telemetry. Positive Z does not necessarily mean the bed moves
up. No motor-current changes, endstop bypass, extrusion or automatic homing.

Nozzle control uses the active nozzle; it does not switch H2D tools. Temperatures
are integer Celsius, with 0 to turn heating off. Limits: A1 mini 300/80°C,
A1 300/100°C, H2D 350/120°C (nozzle/bed). Configure `model` on a printer entry when
its original configured name does not identify the model; management aliases are
not used to infer hardware. Unknown models use conservative 300/80°C limits.
Settings are submitted via MQTT; firmware can reject them. Verify reported
telemetry or the device display. Nonblocking M104/M140 are used; no blocking
heat-and-wait fallback is issued. Sliced G-code can change targets again.

Live view uses one shared camera connection per printer: persistent TLS JPEG for
A1-family cameras, or ffmpeg RTSP decoding for H2D. H2D is limited to 5 fps and
960-pixel width to reduce Pi load; A1 is limited by its camera's low native frame
rate. Notification snapshots share this connection. Only the latest frame is kept
in memory; no video is saved. Multiple viewers share the feed. Streams reconnect
on failure and stop when no viewers/snapshot requests remain. Closing the dialog,
hiding the browser tab, or signing out stops that browser's live view. Camera access
requires dashboard authentication; the endpoint rechecks sessions while streaming.
Firmware LAN permissions, access codes and camera availability still apply.

Validation covers simulated MQTT, permission checks, camera frame parsing and
connection sharing, plus a browser smoke test. Physical hardware has not been
exercised by this update.



## Swap Systems Swapmod A1m — per-printer integration

This integration targets the **Swapmod A1m STL Edition for Bambu A1 mini**:
https://swap-systems.com/product/swapmod/
It replaces the previous Infinity Flow configuration. On update, previous
Infinity Flow settings are disabled and their plate counts cleared; they must
not be reused for a different mechanism. Existing queue jobs are retained.

Enable **Details \& camera → Swapmod A1m (Swap Systems)** only on equipped A1 minis.
Enter the actual blank plate count in the magazine. All other printers retain
their normal manual queue flow. The full-size A1 and H2D are not supported by
this kit. A toggle does not modify the physical mechanism or remove swap G-code
from a file. Previously approved swap files are blocked when the toggle is off.

Use the official Swaplist app for your purchased edition to convert sliced
files into a batch containing the required printing and plate-changing sequence.
Upload that **already converted, sliced .gcode.3mf batch** as one management queue
job. Select the executable plate entry in that generated file, normally plate 1;
verify with your export. Do not upload the unsliced kit assembly project as a job.
The Pi submits this file using the existing printer upload/print workflow. No
FlowQ hub or FlowQ software is involved; it does not log into Swaplist, generate
batches, or inject plate-changing G-code. Vendor files are not bundled here.

On the queue entry choose **Approve Swaplist batch…** and enter the total number
of plates the batch needs. This is operator attestation, not automated G-code
validation. Verify filament/AMS settings for the generated batch. Before starting,
choose **Swapmod starting setup checked…** after following the vendor's starting
instructions and clearing the ejection path. The check deliberately does not tell
you to mount a plate: the generated sequence may load its first plate itself.
Then use the usual Start button or `/queuestart`.

The generated batch controls plate changes within its run; the Pi treats the
whole batch as one job, with aggregate progress. It does not claim to detect
individual plate changes. Separate batches require another starting check and
manual start. Plate counts are estimates: the entire batch's count is reserved
atomically when it is accepted, and uncertain/failed jobs are not auto-refunded.
Inspect and recount before updating inventory. Restarting retains Swapmod settings
and counts but invalidates setup checks and job approvals. Changes to settings
also invalidate approvals; copied jobs require fresh approval.

Discord controls remain administrator/approved-user only, with public confirmation:

* `/plateswap configure name:Steward enabled:true spares:5`
(the model defaults to **A1 mini**)
* `/plateswap approve job\_id:<id> plates:3`
* `/plateswap check name:Steward`
* `/queuestart name:Steward`

Official assembly and Swaplist workflow: https://swap-systems.com/stl/
The STL-edition converter requires the purchaser's login; converted files can run
offline. This update has been tested with mocks and browser checks, not physical
Swapmod hardware or a user-provided converted batch. Supply a sample exported
batch to validate its metadata and executable plate selection before a real run.



## Public help and administrative access

`/help` is a single message containing everyday commands only. `/adminhelp` is a
separate guide for administrators and approved user IDs. It lists
printer controls (including pause/resume/stop, lights, temperatures, speed, fan and
movement), Swapmod, rename, DMs, channel settings/archives, report assignment,
queue starts/edits, MeshCentral laptops and Pi administration. These operations
have a central runtime permission gate; action confirmations also recheck access.
An approved ID does not need the Discord Administrator permission. Configure IDs
in dashboard Settings or the existing `admin\_user\_ids` config. Both paths require
an allowed Discord guild. This controls help output and execution; it does not
promise to hide commands from Discord's native slash-command picker.

Laptop integration now uses MeshCentral's control WebSocket API. The legacy custom
agent routes/enrollment controls are removed, and pending legacy device tasks are
retired without resending. No connection is attempted until MeshCentral is
configured. Do not run the old Python laptop agent alongside this workflow.



## Fans, heated chamber and reply visibility (latest release)

* `/fan percent:50 name:BOB target:part` selects one fan. Choose the printer first to get fan autocomplete from its reported capabilities.
* `/fanall percent:80` changes all manually controllable fans **on every printer**, with confirmation, and reports the result per printer. `/fan` changes one fan on one printer.
* `/chamber degrees:60 name:BOB` or `/temperature target:chamber degrees:60 name:BOB` sets the H2D chamber target; 0 turns heating off, otherwise use 40–65 °C. Firmware can reject heating for the loaded material.
* `/fan` and `/fanall` are open to every member (confirmation required, logged with who ran them). `/chamber` requires an administrator or an approved user ID. All are also available in the dashboard printer details.
* New-protocol fans use reported airduct IDs, allowed ranges and manual-control flags with `set\_fan`. Legacy reports use the supported part/auxiliary/chamber `M106` channels. Hotend heatbreak and electronics fans remain firmware-managed; automatic/off fans are not forced. Change the airflow mode on the printer if needed. A fanall result lists skipped automatic fans.
* Replies now appear publicly in the channel where invoked, regardless of the commands-channel setting. `/publiccommands`, `/dm`, `/assign report` and `/meeting report assign` stay private. Confirmation buttons still belong to their initiator; admin commands recheck administrative access. `/publiccommands` now explains the policy; temporary overrides are unnecessary. Public does not mean broadcasting to every channel.

Printer acknowledgements that report failure appear in Activity and trigger a notification when configured. A submitted MQTT command is not a verified physical result. The original H2D fan error has not been reproduced without its traceback/firmware response. Current mappings were checked against Bambu Studio source; hardware operation still needs verification on the Pi.

Protocol references (Bambu Studio, checked 2026-09-24):

* https://github.com/bambulab/BambuStudio/blob/master/src/slic3r/GUI/DeviceCore/DevFan.cpp
* https://github.com/bambulab/BambuStudio/blob/master/src/slic3r/GUI/DeviceCore/DevFan.h
* https://github.com/bambulab/BambuStudio/blob/master/src/slic3r/GUI/DeviceCore/DevChamberCtrl.cpp
* https://github.com/bambulab/BambuStudio/blob/master/src/slic3r/GUI/DeviceManager.cpp



## Camera and H2D transfer troubleshooting (2026-09-25)

Live view now uses authenticated JPEG frame requests, keeping the upstream camera connection alive between requests and reconnecting automatically. This avoids depending on a browser’s multipart MJPEG support. It is a low-frame-rate live view, roughly one frame per second, not full-motion video. Camera timeout/authentication/ffmpeg failures are displayed and logged with the access code redacted. Use Details \& camera → Start live view; close other printer-camera clients if the printer cannot accept another connection.

H2D uploads now enable the BambuTools client’s TLS unwrap option to close the encrypted data connection cleanly, and allow 60 seconds for individual FTPS operations. Other models retain the existing close behavior. This is a compatibility adjustment, not a confirmed diagnosis of every 426 failure. A printer config can explicitly set `ftp\_tls\_unwrap` true or false. Both a 226 completion reply and matching remote SIZE are still required. A 426 always fails the upload; even a full local byte count does not make an unverified upload printable. No automatic print retry is introduced. Queue errors show transfer stage and bytes sent.

`error\_temp: 426` is an FTP temporary error/aborted transfer, not a temperature reading. If it persists, check that the H2D itself recognizes the inserted USB drive and reports free space, and supply the full queue error plus `sudo journalctl -u 3d-printer-management -n 100 --no-pager`. Do not reformat media containing needed files.

Reference implementation inspected: https://github.com/BambuTools/bambulabs\_api/blob/main/bambulabs\_api/ftp\_client.py
These changes were tested with local HTTP and mocked printer transports, not on a physical H2D/A1 or the user’s browser.

