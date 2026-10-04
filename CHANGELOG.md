# Changelog

What changed in each version. Upgrade steps are in [docs/updates.md](docs/updates.md).

## Unreleased (1.6.1)

### Added
- **Multi-hotend support for dual-nozzle printers (#68): H2D, H2D Pro, H2C, X2D.**
  - **Temperatures:** the dashboard shows a **Left nozzle** and a **Right nozzle** tile, each with its own current → target and settable on its own (Bambu Studio's `set_nozzle_temp`). The nozzle in use is marked. `/temperature` has `left nozzle` / `right nozzle` targets.
  - **Hotends:** the **Nozzle** section shows each side's fitted hotend (size, material, high flow) and what it has loaded, plus the H2C's **hotend rack**. The manual size/type editor is hidden on these printers, because their hotends report themselves.
  - **`/printer`:** shows each nozzle's temperature, hotend and loaded filament, and the hotend rack.
- **Left/right nozzle indicators (#5).** The dashboard's **Filament** section and Discord `/filaments` show which nozzle each AMS and external spool feeds, what each nozzle has loaded and which nozzle is in use. Both external spools are listed and editable (the left one uses `ams_id 254`).
- Single-nozzle printers look and behave as before.

## 1.6.0-beta.1

Built on 1.5.0-beta.1. A better Queue a print dialog with Print now (#7), support for more Bambu Lab printers (#6), printing a finished job on another printer (#57), and fixes for movement commands (#1) and Swapmod (#17).

### Added
- **Print a finished job on another printer (#57, dashboard only).** History jobs have **Print on another printer…**, which adds a copy to a different printer's queue.
  - **Same model only:** only printers of the model the `.3mf` was sliced for are offered. A mismatch is refused, and an unknown model needs a confirmation tick.
  - **Settings:** the AMS mapping is chosen again for the new printer; plate and bed type carry over.
  - **Starting:** the new job still needs the normal start confirmation.
  - **Files on the printer:** for jobs whose file is on the original printer's storage, the file is copied to the Pi when you send it, and uploaded to the new printer at start.
- **Support for more Bambu Lab printers (#6).** A new model registry (`printer_models.py`) covers the A1 mini, A1, A2L, P1P, P1S, P2S, X1, X1 Carbon, X1E, X2D, H2D, H2D Pro, H2S and H2C. For each one it knows the temperature limits, chamber heating, fans, camera type, FTPS settings, serial prefix and Bambu Studio model ID, all taken from Bambu Studio's printer definitions.
  - **Recognising printers:** by `model`, serial number or name, and in sliced files for **Print on another printer**.
  - **Untested:** only the H2D, A1 and A1 mini have been tested; the others are marked untested in [Supported printers](docs/configuration.md#supported-printers).
  - **Your limits:** a printer's nozzle and bed limits can be overridden with `"limits"` in config.json.
  - **Camera hint:** a printer without a camera now gets a hint naming the right `camera_type` for its model.
  - **No change for tested printers:** the H2D, A1 and A1 mini behave exactly as before.
- **Better Queue a print dialog (#7, dashboard).**
  - **Checked on pick:** the `.3mf` is read as soon as you choose it. An unsliced project gets "slice it and export the plate sliced file", and a file sliced for a different printer model is flagged and refused.
  - **Plate picker:** cards for each plate with its thumbnail, name, print time, weight and filament colours, instead of typing a plate number.
  - **AMS suggestion:** the mapping is filled in by matching each filament to a loaded slot of the same material and the closest colour. Mappings can use `-1` for filaments a plate doesn't use, as Bambu Studio does.
  - **Print now:** prints straight away on an idle printer without waiting in the queue, after the usual confirmation. If it can't start, nothing is left in the queue.

### Changed
- **Swapmod is only offered for A-series printers (#17).**
  - **What's hidden:** the dashboard's Swapmod settings and the `/plateswap configure` / `check` printer choices appear only for A1 and A1 mini printers.
  - **Detection:** the configured `model`, or the serial number (`030` = A1 mini, `039` = A1), or the name.
  - **Server checks:** the server refuses to enable or check Swapmod on other printers.
  - **Old settings:** settings saved earlier for a non-A-series printer are switched off at start-up, with a note in the Activity feed.

### Fixed
- **Movement commands that didn't move the printer (#1).** The printer ignores jogs on an axis it reports as not homed (its `home_flag`), for example after it released its motors while idle, so moves were accepted but nothing happened. Like Bambu Studio, the app now reads that flag and asks you to press **⌂ Home** / `/home` first. The dashboard disables the buttons for those axes and explains why. The G-code jog now matches Bambu Studio byte for byte, including its speeds (X/Y at F3000, Z at F900).

## 1.5.0-beta.1

Built on 1.4.0-beta.1. Makes Discord `/printer` on the H2D much lighter on the Pi (#54).

### Fixed
- **Pi crash after `/printer` on the H2D (#54).** `/printer` started a **full live ffmpeg decode** of the H2D's 1080p camera stream, using every CPU core at normal priority, just to get one picture. If the dashboard was open, it could run alongside another full-resolution capture of the same camera. On a Raspberry Pi this could exhaust RAM, CPU or power. Now:
  - **Reuse first:** `/printer` reuses a picture already on hand (an open live view, or the dashboard's still from the last minute).
  - **Otherwise one cheap still:** it takes a single frame: keyframe only, one thread, 960 px, at lower priority.
  - **One capture per camera:** Discord and the dashboard share captures, so a camera never has two at once.
  - **Live view is lighter too:** it runs at lower priority with at most 2 decoder threads.
  - **Reply first:** `/printer` sends the status card straight away and adds the picture when it arrives, instead of waiting up to 25 seconds (part of #44).

## 1.4.0-beta.1

Built on 1.3.0-beta.1. Fixes the dashboard and printer controls becoming unresponsive on 1.3.0-beta.1 (#40).

### Fixed
- **Dashboard unresponsive with A1-family cameras (#40).** 1.3.0-beta.1 kept every printer's camera streaming while the dashboard was open. A1 and A1 mini cameras that didn't answer left browser requests waiting up to 25 seconds each, which used up the browser's connections to the Pi. Control clicks and page refreshes then queued behind them, so buttons stayed purple and nothing reached the printers. Cameras now use staggered still snapshots: one camera at a time, only while the dashboard is open, and the browser never waits on a camera. Live view waits at most 10 seconds for a frame.
- **Whole-service stalls (#40).** An independent thread detects when the shared dashboard and Discord event loop stops making progress. It writes thread stacks to the service journal after 15 seconds and exits after 60 seconds so systemd restarts the service. Uncertain queue jobs still require review after a restart.

## 1.3.0-beta.1

Includes the 1.2.1-beta.1 fixes (commands-channel replies and update checks).

### Added
- **Ubuntu and other Linux distributions (#26).** The installer now runs on any systemd-based Linux as a Raspberry Pi alternative: Ubuntu 24.04+, Debian 12+, Fedora, Arch and openSUSE. It detects the package manager, checks for Python 3.11+ and systemd before changing anything, suggests a LAN address when Tailscale isn't connected, and prints the firewall command to open the dashboard port. See [Installation](docs/installation.md#ubuntu-and-other-linux-distributions).

### Changed
- **Edit AMS filament and nozzle.** In the printer panel, click an AMS slot or the external spool to set its material and colour, as Bambu Studio's *Edit filament* does. A new **Nozzle** row sets the fitted nozzle's diameter and type.
- **Homing.** **⌂ Home** in the dashboard's printer panel and a new admin-only `/home` command home X, Y and Z on an idle printer. It uses Bambu Studio's `back_to_center` where the printer supports MQTT homing, otherwise `G28`.
- **Faster dashboard controls (#37).** In the web dashboard only **Pause** and **Stop** ask for confirmation. Temperatures, speed, fans, light and resume apply straight away, with the same server-side limits. Discord keeps its confirmation cards.
- **Camera stills (#37, #40).** Printer cards and the panel show still snapshots. The Pi takes them one printer at a time, only while the dashboard is open. Live view is started with **▶**. The printer panel's camera area no longer collapses to nothing.
- **Waiting feedback (#37).** A dashboard control you've clicked pulses purple until the printer reports the result, then shows the real state (for example, amber for light on). This covers light, pause/resume/stop, temperature, speed, fans and movement. Purple is only used for waiting.
- **Fans (#37).** Fans are set when you let go of the slider, with no **Set** button, and the slider no longer snaps back. Fans on older firmware (for example the A1 family) show their current speed.
- **Updates only wait for printing (#32).** Updates used to need every printer idle, connected and reporting, and no active or needs-review queue jobs. Now only a printer that's printing (or paused mid-print) or a file being sent to a printer holds an update back. Offline or switched-off printers and waiting queue jobs no longer block updates, manual or automatic. The message names the printer that's blocking.
- **Force update.** Installing from the dashboard while a printer is printing asks you to force the update, explains what happens, and records who forced it in the Activity feed. Automatic updates are never forced.
- **Discord permissions (#21).** Printer actions (`/pause`, `/resume`, `/stop`, `/reprint`, `/lighton`, `/lightoff`, `/speed`, `/fan`) and queue commands (`/queuestart`, `/queueforce`, `/queuemanage`) are open to every member. Each asks for confirmation and is logged with who ran it. `/fanall` now sets fans on **every** printer. Temperatures, chamber heating, movement, Swapmod, DMs, archiving, laptops and the Pi stay admin-only.
- **Command permissions in the dashboard.** **Settings → Discord command permissions** sets each command to Everyone, Allowed roles + admins, Admins only or Off. Commands that reboot the Pi, run laptop commands, send DMs or change channels can only be admin-only or off.
- **Command reference.** [docs/commands.md](docs/commands.md) lists every command and its default permission. A test fails if it goes out of date.

## 1.2.1-beta.1

### Fixed
- **Discord replies are public only in the commands channel (#35).** Commands still work in every channel, but outside the commands channel the reply, confirmation and result are private: only the person who ran the command sees them. `/dm`, `/diagnostics`, `/reportissue`, report assignment, `/notattending` and `/attendance` stay private everywhere. If no commands channel is set, every reply is private, so run `/setcommandschannel` in the channel where replies should be public. To hide the commands elsewhere, limit the bot under Discord's **Server Settings → Integrations → Channels**.
- **Updates only wait for printing (#32).** Updates used to need every printer idle, connected and reporting, and no active or needs-review queue jobs. Now only a printer that's printing (or paused mid-print) or a file being sent to a printer holds an update back. Offline or switched-off printers and waiting queue jobs no longer block updates, manual or automatic. The message names the printer that's blocking.
- **Force update.** Installing from the dashboard while a printer is printing asks you to force the update, explains what happens, and records who forced it in the Activity feed. Automatic updates are never forced.

## 1.2.0-beta.2

### Added
- **Meeting attendance.** `/attending` and `/notattending` (with an optional reason) mark whether you'll be at the next meeting, or a chosen meeting date. Meetings are on the practice/report days. `/attendance` shows who's coming, and the dashboard's **Team tools** tab lists the next six meetings. People who aren't attending are skipped when a report writer is picked, so you aren't chosen to write the report for a meeting you said you'd miss.

### Changed
- **Redesigned printer controls (#3).** The dashboard's printer view is lighter and closer to Bambu Handy:
  - **Printer cards** are compact and open the printer when clicked (or with Enter/Space). They show only the buttons that apply right now (Pause, Resume, Stop, light), with rounded temperatures and AMS colours.
  - **Printer panel:** it slides in from the side, or up from the bottom on phones. It has a progress ring, quick actions, the live camera, tap-to-edit temperature tiles, speed, fan sliders, a movement pad and filament swatches. Files, snapshot and Swapmod are under **More**.
  - **Closing popups:** every popup now closes by clicking outside it or pressing Esc, not just with ✕. Clicking outside a confirmation only cancels that confirmation.
  - **Movement:** one safety checkbox unlocks the movement buttons until the panel closes. The buttons count down the 3-second gap between moves.

## 1.2.0-beta.1

### Changed
- **`/dm` is anonymous (#20).** Messages no longer name the sender: the recipient sees only "Message from \<server\>". The admin-only Activity feed still records who sent each DM.

### Documentation
- **Guides:** the documentation is split into focused guides under `docs/`, and the README is now an overview with links.
- **Changelog:** this changelog replaces the old `Updater/UPDATE.md` history.

## 1.1.0 (beta)

### Fixed
- **H2D missing AMS 0 (#4).** An update about one AMS no longer wipes the others. AMS units and slots are merged by ID, and removed spools show as empty.
- **Movement commands didn't move the printer (#1).**
  - Printers that report MQTT axis control (such as the H2D) now use Bambu's `xyz_ctrl` command, in fixed 1 mm / 10 mm steps.
  - Other printers get G-code wrapped the way Bambu Studio does it.
  - Printer rejections are now reported.
- **Installer:** `install.sh` again installs `configure.py`, and the new modules are included in installs and updates.

### Added
- **Update channels (#18):** stable, beta and alpha, with automatic installation when printers are idle. Switching to a more stable channel never downgrades. The Publish workflow can release beta and alpha pre-releases from any branch.
- **Error logs (#15):**
  - a rotating log file with secrets removed
  - crash capture for background tasks
  - error IDs shown to users
- **Diagnostic reports:** a redacted diagnostic ZIP from the dashboard or `/diagnostics`.
- **Problem reports:** sent to the developers as GitHub issues from the dashboard or `/reportissue`.

### Changed
- **Folder layout:** code is now in packages: `Updater/`, `discord_Intergration/`, `swapMod/` and `laptopManagement_Intergration/`.
- **Manual update script:** now `sudo bash Updater/update.sh` (it was `update.sh`). A v1.0 system needs one manual update to reach 1.1. See [docs/updates.md](docs/updates.md#upgrading-from-v10).
- **Version:** 1.1.0.
- **CI:** CI runs without root, and the test suite has been restored.

## 1.0.0

The first release of the combined dashboard and Discord service. It replaced the standalone Discord bot and imports its settings.

### Printers and queue
- **Shared queue:** a per-printer queue shared by the dashboard and Discord, with sliced `.3mf` uploads or files already on the printer.
  - Starts need confirmation.
  - Uncertain starts become *needs review* instead of retrying.
  - Reprinting reuses the recorded file.
- **Start ignoring error:** `/queueforce` starts a single attempt despite a reported printer error. It doesn't clear the error.
- **Printer controls:**
  - pause, resume and stop
  - chamber light (`/lighton`, `/lightoff`)
  - nozzle, bed and H2D chamber targets
  - speed profiles
  - individual and all-fan control (Bambu Studio fan mapping)
  - axis jogging
- **Printer file browsing:** `/file list`, `/file system` and the dashboard **Printer files** listing, all read-only and bounded.
- **Error descriptions:** official Bambu HMS and print-error descriptions from a bundled offline catalog.
- **Notifications:** progress at every 10 % and on completion, with camera snapshots, without duplicates across reconnects.
- **Camera:**
  - A1-family JPEG and H2D RTSP support
  - more reliable A1 snapshots
  - a shared low-frame-rate live view
- **H2D uploads:** FTPS compatibility adjustments (TLS unwrap, 60-second steps) and stricter upload checks.
- **Swapmod A1m:** plate-swap batch workflow for equipped A1 minis.

### Discord and team tools
- **Permissions:** `/help` and `/adminhelp`, approved user IDs, and a central permission check.
- **Replies** are public except for private workflows.
- **Messaging and admin:** `/dm` with a private preview, `/rename` for printer display names, and `/publiccommands`.
- **Team:**
  - shared notes and DM reminders
  - report-writer assignments with a practice schedule
  - private meeting assignments
  - channel archiving with permission checks
  - `/server` and an opt-in `/reboot`
- **Laptops:** MeshCentral integration, replacing the old Python laptop agent.

### Operations
- **Installer:** migrates the old bot's settings and rolls back if startup fails.
- **Updates:** from an uploaded ZIP or GitHub releases, installed by a root-owned updater with automatic rollback.
- **Demo mode:** simulated printers and jobs.
