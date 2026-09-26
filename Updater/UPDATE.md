# Latest release

This release adds dashboard ZIP updating, new-protocol fan controls, fanall, separate H2D chamber targets, and public channel replies except publiccommands, DM and meeting report assignment. Earlier privacy/override descriptions below are historical and superseded by README.md. See WEB_UPDATES.md for the one-time updater bootstrap.

# Printer controls update

On the hotspot Pi, extract the updated ZIP into a download folder, enter
`printer-management`, and run `sudo bash update.sh`. This updates application
code only and restarts the service; existing config, credentials, queues,
network profiles, systemd units, and watchdog scripts are preserved. New
installations still use install.sh. Refresh the web dashboard afterward.

- Dashboard: **Start ignoring error** beside the first queued job. Requires
  explicit confirmation that the printer, plate, material and sliced file have
  been inspected. Discord: **/queueforce name:<printer>**, with the same owner-only
  confirmation and administrator-or-approved-user restriction as /queuestart.
- The override permits IDLE, FINISH and FAILED with a nonzero reported error.
  It applies only to that start attempt and is recorded in Activity. It does
  not clear printer errors, bypass firmware protections, or fix upload failures.
  Fresh connected telemetry, queue order, and no active/unresolved queue job
  are still required. RUNNING, PAUSE, PREPARE and unknown states remain blocked.
- Resolve a needs_review job after inspecting the printer before starting another.
  Never mark a still-running print complete just to free the queue.
- Dashboard: **Light on / Light off** on each printer card. Discord:
  **/lighton** and **/lightoff**, with the existing fuzzy name picker.
  These send the standard chamber_light MQTT command; support depends on the
  model/firmware. Submitted means queued for delivery, not hardware confirmation.
- /status now defers immediately. Error reporting preserves the original
  traceback if Discord refuses an expired interaction. This does not prove
  the underlying network/interaction timing issue has been resolved.

Validation: 41 automated tests passed, Python and JavaScript syntax checked,
and Discord definitions loaded with fake configuration. Physical printer
lights, error-override starts, and live Discord delivery require on-device testing.
The existing watchdog scripts were not supplied and are not changed by this update.

## Automatic progress photos

Every printer now sends Discord progress notifications at 10%, 20%, …, 90%,
and a detailed completion notification at 100% on FINISH. Each includes the
printer, file, state, progress bar, remaining minutes, layer and temperatures,
and requests a fresh camera snapshot. Camera failure produces a text update
with an explicit unavailable note. Uses the existing notification channel.

Repeated reports/reconnects do not duplicate milestones. Starting the service
mid-print establishes a baseline and begins with the next milestone. If
telemetry skips several milestones, only the newest is sent, using current
status and a current image. Demo queue prints exercise milestones too, with
no camera image. Firmware/camera access still requires testing on your devices.

Validation for this version: 48 automated tests pass, including milestone,
reconnect, pause/resume, same-file reprint, skipped reports and independent-printer
checks. Installer/updater shell syntax and Python compilation pass.

## A1 camera reliability update

The jpeg_tcp path now captures one frame directly over TLS port 6000 without
opening a second printer MQTT session. It correctly reassembles split TCP
headers and frames, bounds image size and capture duration, reconnects once,
and closes sockets after success or failure. Concurrent requests for one
printer share the in-progress snapshot rather than returning unavailable.
The next request after capture completion requests a new frame, not a cached
image. FINISH and IDLE states are supported while printer telemetry reports
connected. RTSP capture is unchanged.

This addresses known code failure paths; it does not establish that the A1's
firmware, network, or camera access settings were the cause of a particular
failure. If the image is still unavailable after installation, inspect:

```bash
sudo journalctl -u 3d-printer-management --since '5 minutes ago' --no-pager | grep -iE 'camera|snapshot'
```

Validation: 56 unit tests passed, plus a concurrency check confirming two
simultaneous callers share one capture and the next call captures afresh.
Physical A1 camera verification is still required. This update retains lights,
queue overrides, progress notifications and existing configuration.


## Team commands, readable errors and printer files

- `/dm user:<ID or @mention> message:<text>`: administrators or configured
  approved user IDs only. Private recipient/message preview, owner-only Send
  button, permission recheck at send time, no duplicate sends from that button.
  The DM identifies the requesting user and server. Discord can refuse DMs due
  to recipient privacy or blocking. Message bodies are not added to Activity.
- `/publiccommands minutes:2`: temporarily make normal replies public in the
  invoking channel, for everyone using commands there. Same admin/approved-ID
  restriction. Use 0 to cancel; default 2, maximum 60 minutes. Private admin/DM
  replies stay private. The override expires automatically, resets on service
  restart, and never deletes previously public messages.
- Printer errors now show official Bambu Studio descriptions for exact codes,
  retaining the code and Bambu support URL. HMS and print_error are decoded
  separately. This is a bundled offline catalog; unknown/ambiguous descriptions
  remain explicitly unknown. See ERROR_SOURCES.md.
- `/archive` and `/unarchive` check the bot's own View Channel, Manage Channels,
  and Manage Roles permissions before changing anything. Failed archive records
  can be retried after permissions are corrected; original restore settings are
  retained. An administrator must grant missing permissions in Discord; code
  cannot grant itself access. Existing protected bot channels remain protected.
- `/file list name:<partial printer name> path:/`: recursively list candidate
  .3mf/.gcode files. `/file system` lists all readable folders/files/links.
  Both send a preview and complete text attachment, up to 2000 entries, depth
  10 and 45 seconds of traversal. Any truncation/inaccessible folders are noted.
  Symlinks are shown but never followed. Supply a smaller path for large storage.
  Listing cannot prove a .3mf is sliced or compatible. Queue a sliced .3mf using
  `/queueadd remote:cache/name.gcode.3mf`; plain .gcode files are listed but are
  not supported by the current managed queue start method.
- Dashboard printer cards have a Printer files download button using the same
  read-only storage scanner. Only authenticated dashboard sessions can use it.

BOB's empty FTP root remains empty without accessible printer storage. Browsing
files does not fix its FTP 553 upload failure. None of these features alter Pi
network/watchdog settings. Use update.sh for an existing installation; refresh
browser after updating and wait for Discord to sync the new commands.

Validation: 72 automated tests pass and all 36 top-level Discord command/group
payloads serialize successfully. New tests cover private DM preview/send/refusal,
permission revocation, temporary-public expiry, archive denial/retry/restore,
error decoding, recursive listings and web authentication. No real DMs or archive
operations were sent during testing; verify on your Discord server and printers.

## Management names and private meeting assignments

- `/rename name:BOB new_name:Workshop Printer`: admins or approved user IDs only;
  private printer selection and confirmation, with permission rechecked before
  saving. Names persist in settings.json. This changes management display names,
  not the printer's on-device name, IP, serial or MQTT connection. Queues,
  telemetry, cameras, history and pending actions retain stable internal keys.
  Both original and display names resolve in commands. Duplicate names and line
  breaks are rejected. Renames display in Discord and dashboard cards, dropdowns,
  queues and activity labels, without restarting the service.
- `/publiccommands` always replies privately, including while a public reply
  override is active. It changes visibility of normal commands, not its own reply.
- `/meeting report assign member:@Person` assigns a specific person. Omit member
  to pick from the configured roster. `/assign report` remains as an alias with
  the same options. Both require an administrator or approved user ID, and their
  success/error responses are always private to the invoker.
- Manual report assignments are saved privately by default (no channel message
  or DM). With `announce:true`, the bot posts a separate assignment announcement
  in the configured practice channel; the command reply remains private. Scheduled
  practice assignments retain their existing public announcement behavior.
- A private specific-person assignment does not require a roster or practice
  channel setting; it validates the chosen member against the invoking server.

77 unit tests pass. Regression coverage includes persistent display aliases,
duplicate-name rejection, stable printer identities, private rename confirmations,
private meeting responses during a public override, specific-person assignments,
and denied access for unapproved users. Physical devices and real Discord messages
were not changed during validation.
