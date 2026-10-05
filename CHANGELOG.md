# Changelog

What changed in each version. Upgrade steps are in [docs/updates.md](docs/updates.md).

## 1.7.2-beta.1

Built on 1.7.1-beta.1. Puts the AI HAT to work: every printing printer is checked every 5 seconds, sensitivity can be changed per printer, flickering failures add up, the AI zooms in on suspicious spots, and new models install safely with automatic rollback.

### Added
- **Check AI now.** A button in each printer's AI panel runs a real check of a printing printer straight away.
  - **Acts on one frame:** you asked for a decision, so if this fresh picture scores at or above the threshold, it acts immediately (pause or notify, as set) instead of waiting for several failing frames over minutes. The alert says it was checked on request.
  - **Good frames:** a frame below the threshold counts as a normal check.
  - **No repeats:** a print already reported isn't paused again.
  - **Confirmation:** asked first when the action is "pause".

- **The AI adapts how often it looks.** With the new default `"interval": "auto"`:
  - **Quiet Pi:** it checks more often, down to every 10 s on the CPU or 5 s with the AI HAT.
  - **Busy Pi:** it backs off, up to every 60 s.
  - **Limit:** it never spends more than half its time checking.
  - **Visible:** the AI panel shows the current pace and why.
  - **Fixed pace:** a number in `"interval"` still works.
- **The failure rule is time-based,** so it means the same at any pace: 60% of the frames from the last 5 minutes have to look failed, over at least 4 minutes (and at least 4 frames). Existing `window`/`needed` settings convert automatically (10 frames × 30 s = 5 minutes, 6 of 10 = 60%).
- **Install a new AI model safely: `failureDetection/install_model.py`.** Give it the Ultralytics Platform's Hailo export (`best.zip`), a `.hef` or a `.onnx`, and it automates what was done by hand on the team Pi:
  - **Checks first:** reads the class names in training order and the on-chip NMS thresholds from the export. With `hailortcli`, it checks the model was compiled for this Pi's chip, with the input and outputs the app expects.
  - **Test run:** it runs the model as the service user with a time limit, optionally on your own failure and healthy pictures (catch rate and false alarms).
  - **Backups and a record:** it backs up the old model and `config.json`, then installs the new one with a record of where it came from. Only the model setting changes, and file owners and permissions are kept.
  - **Verified, or undone:** it restarts the service and confirms the app loaded the model on the AI HAT. Otherwise it rolls everything back. `--rollback` undoes the last install.
- **The log and the AI panel show which processor runs the AI:** `AI model ready: print_failure.hef on AI HAT (Hailo-8)` or `… on CPU`. The model now loads when the service starts instead of at the first check.
- **Fallback to the CPU:** if the AI HAT model won't start (for example, the driver didn't load after a kernel update), the app uses the `.onnx` next to it, at the CPU's pace, instead of stopping AI checks. The AI panel shows a warning.
- **Adjustable AI sensitivity per printer.** A new **Sensitivity** section in each printer's AI panel has presets: Cautious, Normal, Sensitive and Very sensitive. You can also set your own failure score, keep-counting score, how long it must last and the share of frames. A sentence explains what the settings mean, and changes are logged in Activity.
- **Flickering failures add up.** The AI's score for a real failure jumps around the threshold, so a print often never got enough frames over it to be flagged. Now, once two frames reach the failure score, frames at or above a lower keep-counting score (0.15 under it by default) count too.
- **The AI zooms in on the print by itself.** When the model sees anything suspicious, even weakly, the next checks add an enlarged close-up of that spot for 10 minutes. Small failures then score higher. A model trained with a `print` or `bed` class also gets a close-up of the print's area. The AI panel shows 🔍 while zooming.
- **Every printing printer is checked every 5 seconds with the AI HAT.** All printers are now checked at the same time instead of one after another, so the pace doesn't drop with more printers. With a `.hef` model the AI starts at 5 s instead of working down from 30 s. It only slows down if the Pi itself gets busy or a camera is slow. On the CPU the pace is unchanged.

### Fixed
- **The AI judged only about half the frames.** It checks every 30 seconds but reused camera pictures up to 60 seconds old, so every other check saw the frame it had already judged and skipped it. Now it only reuses a picture newer than its check interval, so every check judges a new frame, and failures are confirmed in half the time.

## 1.7.1-beta.1

Built on 1.7.0-beta.1. Adds **Test AI now** for checking the AI on demand, and makes training-picture collection much denser and no longer silent when it can't save.

### Added
- **Test AI now.** A button in each printer's AI panel runs the AI on a fresh camera picture at any time, printing or not.
  - **What it shows:** the picture with coloured boxes around everything it found, each with its confidence, plus where it zoomed in.
  - **Verdict:** whether that frame would count as failing against your threshold.
  - **Training picture:** each test picture is kept for training.
  - **Safe:** it never counts towards the failure rules and never pauses anything.

### Changed
- **Training pictures every minute.** While printing, each printer now keeps a camera still every minute (was every 5) plus every suspicious frame, so training sets fill up much faster. The cap rose to 5,000 pictures (about 1 GB), and nothing is saved when less than 2 GB is free.
- **The AI panel says why training pictures aren't being saved,** for example *"only 1.4 GB free on the Pi"*. Before, it skipped silently.

## 1.7.0-beta.1

Built on 1.6.2-beta.1. A big AI release:
- **Automatic setup:** AI failure detection sets itself up on install (#75).
- **Print file comparison:** it compares what the camera sees with the print file (#79).
- **Automatic reprint:** a paused failure reprints on an available printer whose bed the camera checked is empty (#67).
- **Self-updating:** it keeps its model up to date.
- **Better accuracy:** it looks closer and collects training pictures from your own cameras.

Also new: cancelling single objects during a print (#84), **Print now** that can ignore a reported error, and uploaded files keeping their own name on the printer.

**Updating:** 1.6.2 installs this from the dashboard. Run `sudo bash Updater/update.sh` from the release folder once to also get OpenCV and the AI HAT driver checks set up automatically.

### Added
### Fixed
- **Print now couldn't override a printer error.** After a failed print (FAILED state or an error code), Print now was always refused. The queue's **Start ignoring error** didn't have this problem. The dialog now offers **Print now ignoring error…** while the chosen printer reports FAILED or an error. It asks for the same inspection confirmation, is recorded in Activity, and doesn't clear the printer's error. A printer that's still printing is always refused.
- **Uploaded files keep their own name on the printer.** Files used to be sent as `pm_<id>.gcode.3mf`. They're now sent under the uploaded file's name, for example `Bracket_v2.gcode.3mf`, so the printer's screen, its file list and Discord `/printer` show something recognisable.
  - **Safe names:** spaces and symbols become `_`.
  - **No clashes:** a second waiting job with the same file on the same printer gets its job ID added, so one job can never replace another's file.
  - **Reprints:** keep the original name.

## 1.6.2-beta.1

Built on 1.6.1-beta.1. AI failure detection can now run a trained YOLOv8 `.onnx` model on the Pi's CPU (#70), plus fixes for the AI HAT helper and for dashboard/GitHub updates. **Pis on 1.6.0 or 1.6.1 need one manual `sudo bash Updater/update.sh` to install this release** (see Fixed below).

### Added
- **AI failure detection without an AI HAT (#70).** A YOLOv8 `.onnx` model now runs on the Pi's CPU, for example `best.onnx` straight from training.
  - **No compiling:** no Hailo compiler and no x86 PC are needed. Install `sudo apt install python3-opencv` and point `failure_detection.model` at the `.onnx` file.
  - **Same behaviour:** the same rules, reporting and accurate pausing apply as with the HAT.
  - **Light on the Pi:** OpenCV is limited to 2 threads so the dashboard stays responsive.
  - **Choosing:** a `.hef` model still runs on the AI HAT. The app picks by file extension.

### Fixed
- **AI failure detection: every frame failed with "array is not writeable" (#70).** HailoRT 4.23 refuses the read-only image array the helper passed it; the picture is now copied into a writeable buffer. Found on a Pi 5 with a Hailo-8 AI HAT+ on Raspberry Pi OS Trixie.
- **AI failure detection: "Cannot create log file hailort.log".** The helper now runs from the temp folder, so HailoRT can write its log.
- **1.6.0 couldn't install 1.6.1 from the dashboard or GitHub ("Invalid runtime file in manifest").** The update check only accepted files in a fixed list of folders, so the new `failureDetection/` folder made it refuse the whole release. Nothing was queued, so the panel never moved on.
  - **The fix:** the check now accepts `.py`/`.md` files in any plain package folder. Paths stay restricted: no scripts, hidden folders, `tests/`, or odd names.
  - **Existing Pis:** the check is also root-installed, so Pis on 1.6.0 need one manual `sudo bash Updater/update.sh` to get past this.
- **Dashboard/GitHub updates that rolled back with "Permission denied".** The updater service ran with `UMask=0077`, so the new release's Python environment was readable only by root, and the dashboard service couldn't start it. The update then rolled back.
  - **The fix:** the updater now sets its own umask and makes the whole new release readable by the service before switching to it. New installs get `UMask=0022`.
  - **Clearer errors:** a failed `venv`/`pip` step now logs its last error lines to `sudo journalctl -u pm-web-update`, with credentials in URLs removed. The dashboard still only shows a short message.
  - **Existing Pis:** the updater is root-installed and never replaced by a web update, so this fix arrives with the next manual `update.sh` run (see [Updates](docs/updates.md)).

## 1.6.1-beta.1

Built on 1.6.0-beta.1. Adds multi-hotend support for dual-nozzle printers (#68, #5), remote desktop for MeshCentral laptops (#61) and AI print-failure detection with the Raspberry Pi 5 AI HAT (#70).

### Added
- **Multi-hotend support for dual-nozzle printers (#68): H2D, H2D Pro, H2C, X2D.**
  - **Temperatures:** the dashboard shows a **Left nozzle** and a **Right nozzle** tile, each with its own current → target and settable on its own (Bambu Studio's `set_nozzle_temp`). The nozzle in use is marked. `/temperature` has `left nozzle` / `right nozzle` targets.
  - **Hotends:** the **Nozzle** section shows each side's fitted hotend (size, material, high flow) and what it has loaded, plus the H2C's **hotend rack**. The manual size/type editor is hidden on these printers, because their hotends report themselves.
  - **`/printer`:** shows each nozzle's temperature, hotend and loaded filament, and the hotend rack.
- **Left/right nozzle indicators (#5).** The dashboard's **Filament** section and Discord `/filaments` show which nozzle each AMS and external spool feeds, what each nozzle has loaded and which nozzle is in use. Both external spools are listed and editable (the left one uses `ams_id 254`).
- Single-nozzle printers look and behave as before.
- **Remote desktop for MeshCentral laptops (#61, dashboard).** Each online laptop whose agent supports it has a **🖥 Remote desktop** button in **Devices**.
  - **What it opens:** MeshCentral's own desktop viewer for that laptop, in a new tab. You sign in to MeshCentral with your own account; the dashboard's token never reaches the browser.
  - **Activity:** each session is recorded.
  - **Turning it off:** untick it in the MeshCentral settings.
- **AI print-failure detection with a Raspberry Pi 5 AI HAT (#70).** Off until `failure_detection` is enabled in config.json. See [AI failure detection](failureDetection/FAILURE_DETECTION.md).
  - **How it watches:** while a printer is printing, the Pi takes a camera still every 30 s, using the cheap single-still path, and the AI HAT (Hailo-8L / Hailo-8) scores it with your `.hef` model.
  - **No single-frame alarms:** a print is only flagged when 6 of the last 10 frames fail, spread over at least 4 minutes, with the newest frame still failing. The first 3 minutes of a print aren't judged, frozen or duplicate frames are skipped, and a camera gap starts the evidence again. All of these are configurable.
  - **Reported everywhere:** in Activity, on the printer card and panel, in Discord with the camera picture, and on `/printer`.
  - **Accurate pausing (`"action": "pause"`):**
    - **Once per print:** the pause is sent a single time, and only while the printer still reports RUNNING.
    - **Confirmed:** it's reported as paused only after the printer reports `PAUSE`.
    - **After resuming:** a resumed print isn't paused again.
    - **Default:** with `"notify"`, the default, it only reports.
  - **Per printer:** watching can be turned off for one printer from its panel.
  - **Isolated helper:** the AI runs in a separate helper under the system Python that `hailo-all` installs into. If the HAT, model or library is missing, the dashboard shows *AI HAT unavailable* with the reason, and nothing else is affected.

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
