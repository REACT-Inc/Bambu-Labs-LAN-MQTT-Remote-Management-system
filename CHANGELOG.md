# Changelog

What changed in each version. Upgrade steps are in [docs/updates.md](docs/updates.md).

## Unreleased (1.2.0, in progress on `beta-v1.2.0`)

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
