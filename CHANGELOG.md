# Changelog

What changed in each version. Upgrade steps are in [docs/updates.md](docs/updates.md).

## Unreleased (1.3.0)

### Added
- **Ubuntu and other Linux distributions (#26).** The installer now runs on any systemd-based Linux as a Raspberry Pi alternative: Ubuntu 24.04+, Debian 12+, Fedora, Arch and openSUSE. It detects the package manager, checks for Python 3.11+ and systemd before changing anything, suggests a LAN address when Tailscale isn't connected, and prints the firewall command to open the dashboard port. See [Installation](docs/installation.md#ubuntu-and-other-linux-distributions).

### Changed
- **Homing.** **⌂ Home** in the dashboard's printer panel and a new admin-only `/home` command home X, Y and Z on an idle printer. It uses Bambu Studio's `back_to_center` where the printer supports MQTT homing, otherwise `G28`.
- **Faster dashboard controls (#37).** In the web dashboard only **Pause** and **Stop** ask for confirmation. Temperatures, speed, fans, light and resume apply straight away, with the same server-side limits. Discord keeps its confirmation cards.
- **Camera on by default (#37).** Opening a printer starts its live camera, and the Overview cards show a camera image that refreshes every few seconds. The printer panel's camera area no longer collapses to nothing.
- **Waiting feedback (#37).** A dashboard control you've clicked pulses purple until the printer reports the result, then shows the real state (for example, amber for light on). This covers light, pause/resume/stop, temperature, speed, fans and movement. Purple is only used for waiting.
- **Fans (#37).** Fans are set when you let go of the slider, with no **Set** button, and the slider no longer snaps back. Fans on older firmware (for example the A1 family) show their current speed.
- **Discord permissions (#21).** Printer actions (`/pause`, `/resume`, `/stop`, `/reprint`, `/lighton`, `/lightoff`, `/speed`, `/fan`) and queue commands (`/queuestart`, `/queueforce`, `/queuemanage`) are open to every member. Each asks for confirmation and is logged with who ran it. `/fanall` now sets fans on **every** printer. Temperatures, chamber heating, movement, Swapmod, DMs, archiving, laptops and the Pi stay admin-only.
- **Command permissions in the dashboard.** **Settings → Discord command permissions** sets each command to Everyone, Allowed roles + admins, Admins only or Off. Commands that reboot the Pi, run laptop commands, send DMs or change channels can only be admin-only or off.
- **Command reference.** [docs/commands.md](docs/commands.md) lists every command and its default permission. A test fails if it goes out of date.

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
