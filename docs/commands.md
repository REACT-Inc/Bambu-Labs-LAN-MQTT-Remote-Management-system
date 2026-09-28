# Discord commands and permissions

Every slash command the bot registers, who can run it **by default**, and what else it needs. Administrators can change who can run each command in the dashboard under **Settings → Discord command permissions**. `tests/test_commands_doc.py` fails if a command is added, removed, or changes permission level without this file being updated.

## Who can run what

All commands work only in servers listed in `guild_ids` in `config.json`. They never work in DMs. Each command has one of four permission levels:

| Level | Who can run it |
|---|---|
| **Everyone** | Any member of an authorized server. |
| **Allowed roles + admins** | Admins, plus members with one of the **Allowed role IDs** set in the dashboard. |
| **Admins only** (Admin) | A member with the Discord **Administrator** permission in that server, **or** a user whose ID is in the approved list (`admin_user_ids` in `config.json`, or **Settings → Approved user IDs** in the dashboard). |
| **Off** | Nobody, not even admins. The command is also hidden from `/help` and `/adminhelp`. |

The tables below show the **default** level of each command.

**Changing permissions:** open the dashboard, go to **Settings → Discord command permissions**, pick a level for each command and save.
- Opening an admin command to everyone asks for confirmation first.
- **Reset all to defaults** puts every command back to the levels in this file.
- Changes apply immediately. They're saved in `settings.json` as `command_permissions` (only the commands that differ from their default) and `member_role_ids`.
- Each change is recorded in the Activity feed and the service log.

**Locked commands (🔒):** `/reboot`, `/laptop`, `/laptops`, `/dm`, `/setnotificationchannel`, `/setcommandschannel`, `/archive` and `/unarchive`. These restart the Pi, run commands on laptops, message people as the server, or change channels, so they can only be **Admins only** or **Off**, never opened to everyone or to a role.

**How the check works:**
- Every command goes through one function, `core.command_allowed`. The default levels come from `ADMIN_COMMANDS` in `core.py`, and dashboard settings override them. For groups such as `/plateswap`, the top-level name counts.
- The check runs when the command starts. Most commands run it again when their confirmation button is pressed.
- Confirmation buttons can only be pressed by the person who ran the command, and they expire after 60 seconds. For admin commands, admin access is checked again when the button is pressed.

**Logging:**
- Every command anyone runs is written to the service log (`logs/management.log` / the journal) with the user's name and ID, the command, and its options. Free-text options such as DM or note text are not logged.
- Denied attempts at admin commands are logged as warnings.
- Actions that change a printer or the queue also appear in the dashboard Activity feed, with who ran them.

**Other rules for every command:**
- Commands are refused while a management software update is being installed.
- Replies are **public** in the channel where the command was run. `/dm`, `/publiccommands`, `/assign report`, `/meeting report assign`, `/diagnostics`, `/reportissue`, `/attending`, `/notattending` and `/attendance` always reply privately. "Not allowed" replies follow the same rule.

Parameters marked `?` are optional. `name?` is a printer name, with autocomplete. When it's left out, the bot shows printer selection buttons.

## Everyone

| Command | Parameters | What it does | Notes / limits |
|---|---|---|---|
| `/help` | | Everyday command guide | |
| `/status` | | Bot uptime and every printer's connection | |
| `/printer` | `name?` | Status, temperatures and a camera snapshot | Snapshot depends on model/firmware LAN camera access |
| `/filaments` | `name?` | AMS slots and external spool | |
| `/queue` | `name?` | View a printer's shared queue | |
| `/queueadd` | `name?` `file?` `remote?` `label?` `plate?` `use_ams?` `mapping?` `bed?` | Add a sliced `.3mf` upload, or a file already on the printer, to the queue | Only adds to the queue. Start it with `/queuestart` |
| `/file list` | `name?` `path?` | List printable `.3mf`/`.gcode` files on the printer | Read-only |
| `/file system` | `name?` `path?` | List printer folders and files, with a downloadable listing | Read-only |
| `/ftc` | | FTC Discord invite link | |
| `/website` | | Team website link | |
| `/management` | | Link to the management dashboard | |
| `/rememberthis` | `title` `text` | Save a shared team note | |
| `/remember` | `query?` | Search shared team notes | |
| `/forget` | `note_id` | Delete a note | Only your own notes. **Admins** can delete any note |
| `/remindme` | `minutes` `text` | DM yourself a reminder later | Your DMs must be open to the bot |
| `/reminders` | | Your reminders and their delivery status | Only shows your own |
| `/cancelreminder` | `reminder_id` | Cancel a pending reminder | Only your own |
| `/attending` | `meeting?` | Mark yourself as attending the next meeting, or a chosen meeting date | Private reply. Meetings are the practice days. See [Team tools](team-tools.md#meeting-attendance) |
| `/notattending` | `reason?` `meeting?` | Mark yourself as not attending | Private reply. Only admins see the reason. You're skipped when that day's report writer is picked |
| `/attendance` | `meeting?` | Who is attending, not attending, and hasn't replied | Private reply. Reasons shown to admins only |

### Printer actions and queue (Everyone, with confirmation)

Every command in this table shows a confirmation card first. Only the person who ran the command can press its button, and the action runs only when they confirm. Each confirmed action is written to the dashboard **Activity feed** with who ran it, for example `pause • Discord Alex (123456789012345678)`.

| Command | Parameters | What it does | Notes / limits |
|---|---|---|---|
| `/pause` | `name?` | Pause the current print | |
| `/resume` | `name?` | Resume a paused print | Check the printer is ready first |
| `/stop` | `name?` | Cancel the current print | An active queue job is moved to *needs review* |
| `/reprint` | `name?` | Add the last finished queue job back to the queue | Doesn't start it. Use `/queuestart` after clearing the plate |
| `/lighton` | `name?` | Turn the chamber light on | |
| `/lightoff` | `name?` | Turn the chamber light off | |
| `/speed` | `mode` `name?` | Set the print speed profile | silent / standard / sport / ludicrous |
| `/fan` | `percent` `name?` `target?` | Set one fan on one printer (0–100 %) | |
| `/fanall` | `percent` | Set every manually controllable fan on **every printer** (0 = off) | Reports the result per printer. An offline printer doesn't stop the others |
| `/queuestart` | `name?` | Confirm the plate is clear and start the first queued job | |
| `/queueforce` | `name?` | Start the next job even though the printer reports an error | Doesn't clear printer errors or bypass firmware protections |
| `/queuemanage` | `job_id` `action` | Move a job up/down, remove it, or record the outcome of a job that needs review | Recording an outcome doesn't control the printer |

## Admin

### Printer temperatures and movement

| Command | Parameters | What it does | Notes / limits |
|---|---|---|---|
| `/temperature` | `target` `degrees` `name?` | Set the bed or active nozzle target temperature | Model limits: nozzle ≤300 °C (H2D ≤350), bed ≤80–120 °C by model |
| `/chamber` | `degrees` `name?` | Set the chamber heating target | H2D only. 0 = off, or 40–65 °C |
| `/home` | `name?` | Home X, Y and Z | Asks for confirmation. Printer must be idle, error-free and have no active queue job. 3 s gap shared with `/move` |
| `/move` | `axis` `millimeters` `name?` | Jog an axis | Printer must be idle, error-free, homed and have no active queue job. X/Y ±10 mm, Z ±1 mm. 3 s between moves |

### Swapmod

| Command | Parameters | What it does | Notes / limits |
|---|---|---|---|
| `/plateswap configure` | `name` `enabled` `model?` `spares?` | Enable/disable an installed Swapmod kit and set its magazine plate count | |
| `/plateswap approve` | `job_id` `plates` | Approve a Swaplist batch and its total plate count | |
| `/plateswap check` | `name` | Confirm the starting setup before a batch | Records the check. Sends no movement |

### Server administration

| Command | Parameters | What it does | Notes / limits |
|---|---|---|---|
| `/adminhelp` | | Admin command guide | |
| `/diagnostics` | | Download a diagnostic ZIP (logs, recent errors, printer status) | Private reply. Secrets are removed. See [Troubleshooting](troubleshooting.md) |
| `/reportissue` | `title` `description` | Send a problem report to the developers as a GitHub issue | Private reply. Needs a GitHub token set in the dashboard |
| `/setnotificationchannel` | | Send printer notifications to the current channel | |
| `/setcommandschannel` | | Set the main commands channel | |
| `/publiccommands` | `minutes?` | Explain the reply-visibility policy | Private reply. `minutes` is left over from an older version and is ignored |
| `/rename` | `new_name` `name?` | Rename a printer (in management only, not on the printer) | Asks for confirmation |
| `/dm` | `user` `message` | Send a DM from the bot to a member | Shows a private preview first. The recipient sees "Message from \<server\>" |
| `/archive` | `channel` | Make a channel read-only and move it to Archive | Asks for confirmation. No messages are deleted |
| `/unarchive` | `channel` | Restore an archived channel's category and permissions | Asks for confirmation |
| `/assign report` | `member?` `announce?` | Assign a practice report writer (chosen, or random from the roster) | Private reply. `announce` posts in the practice channel |
| `/meeting report assign` | `member?` `announce?` | Same as `/assign report` | Private reply |

### Laptops and the Pi

| Command | Parameters | What it does | Notes / limits |
|---|---|---|---|
| `/laptops` | | List laptops from MeshCentral | Needs MeshCentral configured in the dashboard |
| `/laptop cmd` | `device_id` `command` | Run a command on a laptop through MeshCentral | Only commands configured on the Pi. Asks for confirmation |
| `/laptop result` | `task_id` | Show a laptop command's result | |
| `/server` | | Pi uptime, free disk space and temperature | |
| `/reboot` | | Reboot the management Pi | Must be enabled locally when installing. Blocked while queue jobs are active. Asks for confirmation |

## Permissions the bot itself needs

Invite the bot with the `bot` and `applications.commands` scopes. In the channels it uses, it needs:

| Discord permission | Needed for |
|---|---|
| View Channel, Send Messages, Embed Links | All replies and notifications |
| Attach Files | Camera snapshots (`/printer`, notifications) and the `/file system` listing |
| Manage Channels, Manage Roles | `/archive` and `/unarchive`, in the channel and the Archive category |

The bot doesn't need the Administrator permission. `/dm` and `/remindme` only work if the recipient allows DMs from server members.

## Changing who counts as Admin

* Discord server **Administrators** always count.
* To let someone run admin commands without making them a Discord administrator, add their user ID under **Settings → Approved user IDs** in the dashboard, or to `admin_user_ids` in `config.json`.
* To give a group of members access to certain commands without making them admins, put their Discord role ID in **Allowed role IDs**, then set those commands to **Allowed roles + admins**.
* To change a command's permission on one install, use the dashboard (see above). To change the **default** for every install, edit `ADMIN_COMMANDS` in `core.py`, then update this file. The test will remind you.
