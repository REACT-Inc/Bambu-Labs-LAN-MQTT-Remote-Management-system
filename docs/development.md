# Development

How the code is organised, how to run it locally, and how changes are tested and released.

- [Code layout](#code-layout)
- [Running locally](#running-locally)
- [Tests](#tests)
- [Continuous integration](#continuous-integration)
- [Adding a new file](#adding-a-new-file)
- [Branches and pull requests](#branches-and-pull-requests)

## Code layout

| Path | Responsibility |
|---|---|
| `main.py` | Entry point: builds the store, queue engine, dashboard and Discord commands, connects printers, runs the web server and bot |
| `core.py` | Configuration, MQTT printer connections and telemetry, the Discord bot and core commands, permissions, notifications |
| `dashboard.py` | aiohttp web app: login/sessions/CSRF, the JSON API used by `static/`, settings |
| `queueing.py` | Durable SQLite queue and activity log (`Store`), and the start/monitor/resolve logic (`Engine`), including FTPS upload |
| `printer_controls.py` | Validated printer controls (temperatures, speed, fans, chamber, movement) shared by Discord and the dashboard |
| `thermal_controls.py` | Fan capability mapping (from Bambu Studio) and chamber control |
| `printer_files.py` | Read-only, bounded browsing of printer storage over FTPS |
| `printer_errors.py`, `bambu_error_catalog.json` | Offline Bambu HMS and print-error descriptions. See [ERROR_SOURCES.md](../ERROR_SOURCES.md). |
| `camera_capture.py`, `live_camera.py` | Single-frame capture and shared live camera connections |
| `progress_notifications.py` | Progress milestone de-duplication |
| `team.py` | Team notes, reminders, assignments, practice scheduler, channel archive, server status/reboot |
| `diagnostics.py` | Rotating log file, error IDs, redacted diagnostic ZIP, `/diagnostics` |
| `issue_reports.py` | Problem reports sent as GitHub issues, `/reportissue` |
| `configure.py` | Creates `config.json` at install time (imports the old bot) and the initial password |
| `discord_Intergration/` | Discord commands: queue (`discord_queue.py`), controls, team tools, DMs/rename/file browsing (`extra_discord.py`) |
| `swapMod/` | Swapmod A1m plate-swap logic and its Discord commands |
| `laptopManagement_Intergration/` | MeshCentral client and [LAPTOPS.md](../laptopManagement_Intergration/LAPTOPS.md) |
| `Updater/` | Version (`version.py`), GitHub and ZIP updates, package validation, the root-owned worker, `update.sh`, `install-web-updater.sh` |
| `static/` | Dashboard front end: plain HTML, CSS and JavaScript with no build step, served under a strict Content-Security-Policy (no inline scripts or styles) |
| `install.sh`, `build_release.py` | Installer and release-ZIP builder |
| `tests/` | `unittest` test suite |

## Running locally

A local run needs Python 3.11+. Demo mode runs without printers or Discord:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p /tmp/pm-dev
echo '{"demo": true, "port": 8080, "listen": ["127.0.0.1"]}' > /tmp/pm-dev/config.json
PM_CONFIG=/tmp/pm-dev/config.json PM_DATA=/tmp/pm-dev .venv/bin/python -c \
  "import json,dashboard,pathlib; pathlib.Path('/tmp/pm-dev/auth.json').write_text(json.dumps(dashboard.password_hash('dev-password-123')))"
PM_CONFIG=/tmp/pm-dev/config.json PM_DATA=/tmp/pm-dev .venv/bin/python main.py
```

Then open <http://127.0.0.1:8080> and sign in with `dev-password-123`.
- **Printer data:** add `example_data` to the config to show realistic printers (temperatures, AMS, printing state).
- **Discord:** add `discord_token` and `guild_ids` to test Discord.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -q
```

The suite covers:
- queue persistence and ordering, restart recovery, matching telemetry, stale data and stop-during-staging
- file validation
- sessions, CSRF and origin checks
- Discord permissions, command registration and ID precision
- printer controls and movement payloads, the AMS merge, camera parsing
- the updater and GitHub channels, logging and redaction, problem reports
- a check that the install and update scripts copy every runtime file

Some updater tests change file ownership. They run fully as root, and skip the ownership step when not running as root, as in CI.

## Continuous integration

`.github/workflows/tests.yml` runs on every pull request and every push to `main`, on Python **3.12 and 3.13**:
- unit tests
- `bash -n` on the shell scripts
- `node --check` on `static/*.js`
- a test release build

`.github/workflows/release.yml` publishes releases. See [Updates & releases](updates.md#publishing-a-release).

## Adding a new file

- **New Python module:** add it to the file list in `install.sh`, and to both `PM_FILES` and the syntax-check list in `Updater/update.sh`. `tests/test_install_lists.py` fails if you forget.
- **Files in a new folder:** they're only packaged if the folder is listed in `PACKAGE_DIRS` in `Updater/update_package.py` (for `.py`/`.md`) or is `static/`.
- **New dashboard asset:** add it to the allowed list in `Dashboard.asset` (`dashboard.py`) and to `PM_FILES` in `Updater/update.sh`.
- **New Discord command:** make sure it's covered by `/help` or `/adminhelp` and by the permission rules in `core.py` (`ADMIN_COMMANDS`).

## Branches and pull requests

- **Branches:** work happens on beta branches (for example `beta-v1.2.0`). Each change is a pull request into the current beta branch, and stable releases are merged to `main`.
- **Before opening a PR:** run the tests. If you touched `static/`, also run `node --check`.
- **Versions:** don't edit `Updater/version.py` by hand for a release. `build_release.py` writes it.
