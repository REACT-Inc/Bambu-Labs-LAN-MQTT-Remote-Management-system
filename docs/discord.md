# Discord bot

The bot runs inside the same service as the dashboard and uses the same printers, queues and history. If `discord_token` is empty or Discord is down, the dashboard keeps working.

- [Setup](#setup)
- [Who can use what](#who-can-use-what)
- [Using commands](#using-commands)
- [Command reference](#command-reference)
- [Notifications](#notifications)
- [Reply visibility](#reply-visibility)

## Setup

1. **Create the bot:** create a bot in the [Discord Developer Portal](https://discord.com/developers/applications). Invite it with the `bot` and `applications.commands` scopes.
2. **Configure it:** put the token in `discord_token` and your server ID(s) in `guild_ids` in [`config.json`](configuration.md), then restart the service. Slash commands sync when the bot connects, which can take a minute to show up.
3. **Set the channels:** in the channel for automatic updates, run `/setnotificationchannel`. In the channel where people should use printer commands, run `/setcommandschannel`: replies are public there and private everywhere else (see [Reply visibility](#reply-visibility)). Both can also be set in dashboard **Settings**.
4. **Grant these permissions to the bot:**

   | Permission | Needed for |
   |---|---|
   | View Channel, Send Messages, Embed Links | Everything |
   | Attach Files | Camera snapshots, file listings, diagnostic reports |
   | Manage Channels, Manage Roles | `/archive` and `/unarchive` only |

   It doesn't need the Administrator permission.

## Who can use what

- **Allowed servers:** commands only work in servers listed in `guild_ids`, never in DMs.
- **Admin:** a member with Discord's **Administrator** permission in that server, **or** a user ID in the approved list (dashboard **Settings → Approved user IDs**, or `admin_user_ids` in `config.json`).
- **Everyone, with confirmation and logging:** printer actions (pause, resume, stop, reprint, lights, speed, fans) and the queue (start, force start, manage) are open to every member. Each asks for confirmation and is recorded in the Activity feed with who ran it.
- **Changing who can use a command:** admins can set each command to **Everyone**, **Allowed roles + admins**, **Admins only** or **Off** in the dashboard under **Settings → Discord command permissions**. Commands that reboot the Pi, run laptop commands, send DMs or change channels can only be admins-only or off. The full list with default levels is in [Commands and permissions](commands.md).
- **Enforcement:**
  - **When a command starts:** admin-only commands are checked centrally.
  - **When a button is pressed:** most are checked again, so revoking someone's access takes effect at once.
- **Discord's own settings:** server-side Discord settings (**Server Settings → Integrations**) can hide or block commands on top of this. Check there if an approved user can't see a command.

`/help` lists the everyday commands, and `/adminhelp` lists the admin commands.

## Using commands

- **Printer names are forgiving.** The `name` option accepts the full name, part of it, or a small typo. If it's unclear, or you leave it out, the bot shows printer buttons to pick from.
- **Buttons belong to you.** Only the person who ran a command can press its buttons, and they expire after 60 seconds.
- **Anything that changes a printer or the queue asks for confirmation first.**
- **"Submitted" isn't "done".** It means the command was sent to the printer, not that the printer did it. Watch the printer's status.
- **Error IDs.** If a command fails, the reply includes an **error ID**. See [Troubleshooting](troubleshooting.md).

## Command reference

**E** = everyone in an allowed server, **A** = admin (see above), by default. `?` marks an optional option. Full details and notes per command: [Commands and permissions](commands.md).

### Printers and files

| Command | Who | What it does |
|---|---|---|
| `/help` | E | Everyday command guide |
| `/status` | E | Bot uptime and every printer's connection |
| `/printer name?` | E | Status, temperatures, progress and a camera snapshot |
| `/filaments name?` | E | AMS slots and external spool |
| `/file list name? path?` | E | Printable `.3mf`/`.gcode` files on the printer (read-only, with a text attachment) |
| `/file system name? path?` | E | Every folder and file on the printer's storage (read-only) |

The file listings go up to 10 folders deep, list up to 2000 entries, and stop after 45 seconds. Symlinks are listed but not followed.

### Print queue

| Command | Who | What it does |
|---|---|---|
| `/queue name?` | E | Show a printer's waiting jobs |
| `/queueadd name? file? remote? label? plate? use_ams? mapping? bed?` | E | Add a job: attach a sliced `.3mf` **or** give `remote:` (a path already on the printer, e.g. `cache/part.gcode.3mf`) |
| `/queuestart name?` | E | Confirm the plate is clear and start the first waiting job |
| `/queueforce name?` | E | Start the next job even though the printer reports an error |
| `/queuemanage job_id action` | E | `up`, `down`, `remove`, or record an outcome: `resolve_finished` / `resolve_failed` / `resolve_cancelled` |
| `/reprint name?` | E | Add the most recent finished queue job back to the queue (it isn't started) |

Details: [Print queue](print-queue.md).

### Printer controls

| Command | Who | What it does |
|---|---|---|
| `/pause name?` · `/resume name?` | E | Pause or resume the current print |
| `/stop name?` | E | Cancel the current print (confirmation) |
| `/lighton name?` · `/lightoff name?` | E | Chamber light |
| `/temperature target degrees name?` | A | `nozzle`, `bed` or `chamber` target in °C (0 = off) |
| `/chamber degrees name?` | A | H2D chamber target: 0 = off or 40–65 °C |
| `/speed mode name?` | E | `silent`, `standard`, `sport` or `ludicrous` |
| `/fan percent name? target?` | E | One fan, 0–100 % (autocomplete lists the printer's fans) |
| `/fanall percent` | E | All manually controllable fans on **every printer** (reports the result per printer) |
| `/home name?` | A | Home all axes (idle printer only) |
| `/move axis millimeters name?` | A | Jog X, Y or Z. Needs an idle, homed printer. |

Limits and safety rules: [Printer controls](printer-controls.md).

### Swapmod

| Command | Who | What it does |
|---|---|---|
| `/plateswap configure name enabled model? spares?` | A | Turn the kit on/off for a printer and set the magazine plate count |
| `/plateswap approve job_id plates` | A | Approve a Swaplist batch and the total plates it needs |
| `/plateswap check name` | A | Record that the starting setup was checked |

Details: [Swapmod](swapmod.md).

### Team

| Command | Who | What it does |
|---|---|---|
| `/ftc` · `/website` · `/management` | E | Links configured in dashboard Team tools |
| `/rememberthis title text` · `/remember query?` | E | Save / search shared notes for this server |
| `/forget note_id` | E | Delete your own note (admins can delete any) |
| `/remindme minutes text` · `/reminders` · `/cancelreminder reminder_id` | E | Personal DM reminders |
| `/attending meeting?` · `/notattending reason? meeting?` | E | Say whether you'll be at the next meeting, or a chosen meeting date |
| `/attendance meeting?` | E | Who is attending, not attending and hasn't replied (only admins see reasons) |
| `/assign report member? announce?` | A | Assign a practice report writer (chosen, or picked from the roster) |
| `/meeting report assign member? announce?` | A | Same as `/assign report` |
| `/archive channel` · `/unarchive channel` | A | Make a text channel read-only and move it to Archive, or restore it |
| `/dm user message` | A | Send a DM from the bot after a private preview. The recipient isn't told who sent it. |

Details: [Team tools](team-tools.md).

### Administration

| Command | Who | What it does |
|---|---|---|
| `/adminhelp` | A | Admin command guide |
| `/setnotificationchannel` · `/setcommandschannel` | A | Use the current channel for notifications or commands |
| `/rename new_name name?` | A | Change a printer's display name (dashboard and Discord only; history is kept) |
| `/publiccommands minutes?` | A | Show the reply-visibility policy and the current commands channel. `minutes` is ignored; it's left over from an older version. |
| `/diagnostics` | A | Private diagnostic ZIP (logs, errors, printer status). See [Troubleshooting](troubleshooting.md). |
| `/reportissue title description` | A | Send a problem report to the developers as a GitHub issue |
| `/laptops` · `/laptop cmd device_id command` · `/laptop result task_id` | A | MeshCentral laptops. See [LAPTOPS.md](../laptopManagement_Intergration/LAPTOPS.md). |
| `/server` | A | Pi hostname, uptime, free disk space and temperature |
| `/reboot` | A | Reboot the Pi (only if enabled at install, and not during active jobs) |

## Notifications

The bot posts to the **notification channel** when:

| Event | Includes a camera snapshot |
|---|---|
| Print started, paused, resumed, preparing, idle | — |
| Progress at 10 %, 20 %, … 90 % | yes |
| Print complete (100 %) and print failed | yes |
| Printer error appears / clears, and HMS health alerts | error: yes |
| Printer went offline / came back online | — |
| A job was queued, and control rejections from the printer | — |

How repeats are handled:
- **No duplicates:** repeated or reconnecting reports don't post the same milestone twice.
- **Starting mid-print:** if the service starts in the middle of a print, it starts from the next milestone.
- **Skipped milestones:** if several were skipped, only the newest is posted.
- **Camera failures:** if the camera can't be reached, the notification is still sent, with a note saying so.

## Reply visibility

- **In the commands channel** (set with `/setcommandschannel` or in dashboard **Settings**): replies are **public**, so everyone in the channel sees them.
- **In any other channel:** commands still work, but replies are **private**. Only the person who ran the command sees them, and nothing is posted in that channel. This covers the reply, confirmation cards, printer buttons and the result.
- **No commands channel set:** every reply is private.
- **Always private, even in the commands channel:**
  - `/dm`
  - `/publiccommands`
  - `/assign report` and `/meeting report assign` (the optional `announce` posts separately in the practice channel)
  - `/diagnostics` and `/reportissue`
  - `/notattending` and `/attendance`
- **Unchanged:** printer notifications still go to the **notification channel**.

**Hiding the commands in other channels (optional):** the bot can't remove its commands from Discord's command list. A server admin can do that in **Server Settings → Integrations → *the bot* → Channels**, by allowing only the commands channel.
