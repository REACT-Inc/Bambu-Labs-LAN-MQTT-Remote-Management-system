# Updates & releases

There are three ways to update an installed system:

| Method | Best for | Where |
|---|---|---|
| **GitHub releases** | Normal use, including automatic updates | Dashboard **Settings → GitHub releases** |
| **Upload a ZIP** | A release ZIP you downloaded or built | Dashboard **Settings → Software update** |
| **Manual script** | First install of the updater, or when the dashboard is unreachable | `sudo bash Updater/update.sh` on the Pi |

Every method keeps `config.json`, the queue database, uploads and settings, and restores the previous version if the new one fails its startup check.

- [Before updating](#before-updating)
- [When updates wait, and forcing one](#when-updates-wait-and-forcing-one)
- [GitHub releases and update channels](#github-releases-and-update-channels)
- [Uploading a release ZIP](#uploading-a-release-zip)
- [Manual update on the Pi](#manual-update-on-the-pi)
- [Upgrading from v1.0](#upgrading-from-v10)
- [Publishing a release](#publishing-a-release)

## Before updating

- **Printers:** updates wait while a printer is printing (see [below](#when-updates-wait-and-forcing-one)). Idle, finished, failed and offline printers don't hold them up. While installing, dashboard changes and Discord commands are paused.
- **Disk space:** keep at least **1 GiB** free on the application disk.
- **Internet:** needed to install Python packages.
- **Trust:** only install releases you trust. Installed code can read the printer, Discord and MeshCentral credentials.

## When updates wait, and forcing one

An update restarts this service, **not the printers**. A printer keeps printing from its own storage while the dashboard and Discord are offline for about a minute. So only these hold an update back:

| Situation | Manual update | Automatic update |
|---|---|---|
| A connected printer is printing, preparing or paused mid-print | Waits (or force it) | Waits |
| A print file is being sent to a printer (queue job *staging*) | Waits (or force it) | Waits |
| An **offline** printer has a queue job marked printing or paused | Goes ahead | Waits, because nobody can tell whether it's still printing |
| Printers idle, finished, failed, offline or switched off | Goes ahead | Goes ahead |
| Queue jobs waiting to start, queued, or needing review | Goes ahead | Goes ahead |

The message says exactly which printer is holding the update back, and **Settings → Software update** shows it too.

**Forcing an update:** if you install (from **Install reviewed update…** or **Install release…**) while something is printing, the confirmation turns into **Force the software update?** and lists what's printing. Forcing:
- **Keeps prints going** on the printers, but progress notifications during the restart may be missed.
- **Moves active queue jobs to *needs review*** after the restart, so check each printer and record the outcome.
- **Cuts off** a print file that's still being sent to a printer.
- **Is recorded** in the Activity feed and the log, with who forced it (the dashboard session's address) and what was printing.

Forcing never overrides another update in progress, missing web-updater setup, low disk space or a checksum failure. **Automatic updates are never forced.** They wait until nothing is printing.

## GitHub releases and update channels

**Settings → GitHub releases:**

1. **Repository:** `owner/name`, for example `REACT-Inc/Bambu-Labs-LAN-MQTT-Remote-Management-system`. A **token** is only needed for private repositories.
2. **Update channel:**

   | Channel | Installs | Example tags |
   |---|---|---|
   | **Stable** (default) | Stable releases only | `v1.2.0` |
   | **Beta** | Beta and stable releases | `v1.2.0-beta.1` |
   | **Alpha** | Alpha, beta and stable releases | `v1.2.0-alpha.1` |

3. **Automatically install newer releases when no printer is printing** (optional). Or use **Check now → Install release…** by hand.

How it behaves:
- **Checking:** it checks GitHub about once an hour and picks the **newest release on your channel**. For the same version number, alpha < beta < stable, so `1.2.0-alpha.3` < `1.2.0-beta.1` < `1.2.0`.
- **No downgrades:** switching to a more stable channel never downgrades. A Pi on `1.2.0-beta.2` that switches to Stable stays there until `1.2.0` or newer is released.
- **Verification:** each download is checked against the release's SHA-256 checksum, and the package's own version must match the tag.
- **Failed automatic installs** aren't retried automatically. Review what went wrong, then retry by hand or wait for a newer release.

## Uploading a release ZIP

**Settings → Software update:**
1. Choose the release ZIP (up to 32 MiB) and **Upload & review**.
2. Check the version, file count and SHA-256 shown.
3. **Install reviewed update…**, then confirm. If a printer is printing, the confirmation asks you to force the update (see [above](#when-updates-wait-and-forcing-one)).

The dashboard disconnects while the service restarts. Sign in again to see the result. ZIPs without an `update-manifest.json` (anything not built with `build_release.py`) are rejected.

## Manual update on the Pi

```bash
unzip 3d-printer-management.zip -d ~/pm-new    # use a new, empty folder
cd ~/pm-new/printer-management
sudo bash Updater/update.sh
```

What the script does:
1. It checks every file is present and every Python file is valid.
2. It backs up the current code to `/var/backups/pm-controls-*`.
3. It stops the service, installs the new files and the web updater, and starts the service again.
4. It restores the backup if the dashboard doesn't come back within 30 seconds.

It updates application code only. Configuration, data, the Python environment and system packages aren't touched.

## Upgrading from v1.0

**v1.0 can't update itself to 1.1 or later:**
- Its updater only installs stable releases.
- It rejects the new folder layout.

So neither the GitHub updater nor ZIP upload works from v1.0. You need to do **one manual update** on the Pi:

1. **Get the release ZIP:** download the release ZIP (`3d-printer-management.zip`), or build one with `python3 build_release.py v1.1.0-beta.1 out.zip`.
   - Don't use GitHub's **Code → Download ZIP**. The version would read `1.1.0` instead of the beta version, and beta updates would then be skipped.
2. **Unzip into a new, empty folder.** The old v1.0 download (often `~/printer-management`) doesn't contain `Updater/`.
3. **Check the version:** `cat Updater/version.py` should show the release version.
4. **Install:** run `sudo bash Updater/update.sh`. In v1.0 the command was `sudo bash update.sh`.
5. **Set up automatic updates:** in **Settings → GitHub releases**, set the repository and channel and turn on automatic updates.

After this one manual update, future updates install from the dashboard or GitHub as usual.

## Publishing a release

Releases are built and published by the **Publish release** GitHub Actions workflow (*Actions → Publish release → Run workflow*):

| Tag | Run it from | Result |
|---|---|---|
| `vX.Y.Z` (stable) | `main` only | GitHub "Latest" release |
| `vX.Y.Z-beta.N` / `vX.Y.Z-alpha.N` | any branch, e.g. `beta-v1.2.0` | GitHub **pre-release** (stable installs never see it) |

The workflow:
1. runs the tests and syntax checks
2. builds `3d-printer-management.zip` plus its `.sha256` file
3. builds the [desktop app](desktop-app.md) for Windows laptops on a Windows machine, tests it end to end, and adds `3d-printer-management-desktop.exe` plus its `.sha256` file
4. publishes them

**Never reuse a tag.**

To build a ZIP locally without publishing:

```bash
python3 build_release.py v1.2.0-beta.1 /absolute/path/3d-printer-management.zip
```

The build writes the version into `Updater/version.py` and generates `update-manifest.json`. It excludes caches, local configuration, virtual environments and uploads.

How the updater installs and rolls back is described in [WEB_UPDATES.md](../Updater/WEB_UPDATES.md).
