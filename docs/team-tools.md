# Team tools

Robotics-team helpers shared between Discord and the dashboard's **Team tools** tab. Records are kept per Discord server (only servers in `guild_ids`).

- [Team links](#team-links)
- [Shared notes](#shared-notes)
- [Reminders](#reminders)
- [Report-writer assignments and practice schedule](#report-writer-assignments-and-practice-schedule)
- [Channel archiving](#channel-archiving)
- [Direct messages](#direct-messages)
- [Server status and reboot](#server-status-and-reboot)

## Team links

`/ftc`, `/website` and `/management` reply with links you set in **Team tools**: the FTC Discord invite, the team website and the dashboard URL. Nothing is guessed. If a link isn't set, the command says so.

## Shared notes

| Command | Who | Effect |
|---|---|---|
| `/rememberthis title text` | everyone | Save a note for this server |
| `/remember query?` | everyone | Show the 5 newest notes matching the search (all notes if empty) |
| `/forget note_id` | owner, or admins | Delete a note |

The dashboard can view, add and delete notes for every allowed server.

## Reminders

| Command | Effect |
|---|---|
| `/remindme minutes text` | The bot DMs you after that many minutes (up to one year) |
| `/reminders` | Your reminders and whether they were delivered |
| `/cancelreminder reminder_id` | Cancel one of your pending reminders |

- **Surviving restarts:** reminders are saved, so a service restart doesn't lose them.
- **DMs must be open:** the recipient must allow DMs from server members. A failed delivery is recorded; it **never** falls back to posting in a public channel.
- **Uncertain deliveries:** if the service crashes while delivering, the reminder is marked for review rather than resent, because the app can't know whether Discord accepted it.
- **From the dashboard:** admins can schedule a reminder for any Discord user ID and see all stored reminders.

## Report-writer assignments and practice schedule

- **Assign a writer:** `/assign report` (or `/meeting report assign`) picks someone to write the practice/meeting report. Admins only; the reply is private.
  - **`member:`** assigns that person. Otherwise the bot picks from the **roster**, **least-used members first**, so everyone gets a turn before anyone repeats.
  - **`announce:true`** also posts the assignment in the practice channel. Without it, the assignment is only saved (no post, no DM).
- **Automatic practice schedule** (dashboard **Team tools → Practice & report assignments**):

  | Setting | Example |
  |---|---|
  | Time zone | `America/New_York` |
  | Practice days | `0,2,4` (0 = Monday … 6 = Sunday) |
  | Practice time | `16:00` (local) |
  | Practice channel | Channel ID where assignments are posted |
  | Team role (optional) | Role ID to mention |
  | Roster | Discord user IDs who can be picked |

  - **Turning it on:** tick **enable automatic announcements** after saving. It needs a roster, days and a channel.
  - **One per practice:** one assignment is stored per scheduled practice.
  - **Missed practices:** there's a 20-minute catch-up window; older missed practices are skipped. Turn the schedule off for holidays.
  - **Discord permissions:** the bot needs access to the practice channel, and permission to mention the role if the role isn't mentionable.
- **Keep the service running.** Assignments and reminders are sent by this service, so the Pi has to stay on and connected to Discord.

## Channel archiving

`/archive channel` and `/unarchive channel` (admins, with confirmation):

- **Archiving:** saves the channel's category and permission overrides, moves it to **Archive**, and makes it read-only for non-administrators. Viewing is unchanged, and Discord Administrators can still write. **No messages are deleted.**
- **Restoring:** puts back the original category and permissions.
- **Bot permissions:** the bot needs **View Channel, Manage Channels and Manage Roles** in the channel and in the Archive category. It checks these first and names any that are missing.
- **Failures:** a failed or interrupted archive keeps the saved snapshot, so you can fix permissions and retry. Don't change the saved record by hand.
- **Protected channels:** the bot's own channels and the practice channel can't be archived.
- **Limits:** deleted roles or members can't be restored.

## Direct messages

`/dm user message` (admins) sends a DM from the bot:
- **Preview first:** it shows a private preview with **Send DM**, and access is checked again when you press it.
- **What the recipient sees:** only "Message from \<server\>" and the message. **The sender isn't shown.** The preview reminds you of this.
- **Logging:** the admin-only Activity feed still records who sent a DM to whom (but not the text), so misuse can be traced.
- **Refusals:** Discord can refuse a DM because of the recipient's privacy settings or a block. The reply says so.

## Server status and reboot

- **Status:** `/server` and the dashboard **Server** tab show the Pi's hostname, uptime, free disk space and CPU temperature.
- **Reboot:** `/reboot` and **Reboot Pi…** restart the **Pi** (not a printer), after confirmation. They're off until enabled with `install.sh --enable-reboot`, and refused while queue jobs are staging, waiting to start, printing or paused. See [Installation](installation.md#optional-allow-rebooting-the-pi-from-discord-or-the-dashboard).
