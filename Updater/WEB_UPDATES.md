# Web updater internals

How dashboard and GitHub updates are installed. For how to *use* updates, see [docs/updates.md](../docs/updates.md).

## Components

| Component | Runs as | Role |
|---|---|---|
| Dashboard (`Updater/web_updates.py`, `Updater/github_updates.py`) | `printermanager` | Accepts an uploaded or downloaded ZIP, validates it, and places it plus a `request.json` in `/var/lib/3d-printer-management/updates/` |
| `pm-web-update.path` | systemd | Starts the worker when `request.json` appears |
| `pm-web-update.service` → `/usr/local/lib/pm-updater/worker.py` | root | Installs the release and rolls it back on failure |
| `Updater/update_package.py` | both | Validates packages (the same code is installed root-owned for the worker) |

The worker is installed by `Updater/install-web-updater.sh`, which `install.sh` and `Updater/update.sh` run. **Changes to the worker itself only take effect after a manual `install.sh` or `Updater/update.sh`.**

## Package validation

A release ZIP must:
- **Have the right root:** contain a `printer-management/` folder with `update-manifest.json` (format 1, application `3d-printer-management`).
- **Match the manifest:** list every runtime file with its SHA-256; files not in the manifest are ignored.
- **Be safe:** have no absolute paths, `..`, symlinks, special or encrypted entries.
- **Fit the limits:** at most 1500 entries and 128 MiB expanded (32 MiB compressed).
- **Parse:** every `.py` file must be valid Python.
- **Have a plain `requirements.txt`:** package names and version constraints only.

The manifest detects corruption; it **isn't a signature**. GitHub downloads are also checked against the release's `.sha256` asset.

## Install sequence

1. **Validate again** as root, and check there's at least 1 GiB free.
2. **Prepare a new release folder** in `/opt/3d-printer-management-releases/<id>/`, with its own Python environment. The packages are installed as `printermanager`. If this fails, the running version is untouched.
3. **Stop the service** and back up the configuration and data to `/var/lib/pm-updater/backups/<id>/`.
4. **Switch versions:** point `/opt/3d-printer-management` at the new release, record a journal, and start the service.
5. **Health check:** poll the dashboard health endpoint several times. On failure, **restore the previous code, configuration and data**, and report `rolled_back`.

Uploaded print files stay in place, and queue jobs are never restarted automatically. The health check covers the dashboard starting, not Discord, cameras or printers.

**Interrupted switch:** if the worker is interrupted mid-switch (power loss), the journal makes the updater recover at the next boot.

## Status and recovery

```bash
sudo systemctl status pm-web-update 3d-printer-management --no-pager
sudo journalctl -u pm-web-update -n 60 --no-pager
cat /var/lib/pm-updater/status.json
```

- **If status says recovery is required:** read the log, fix the reported problem, then run `sudo systemctl restart pm-web-update`. Don't delete the journal to skip recovery.
- **Disk space:** backups (`/var/lib/pm-updater/backups`) and old releases (`/opt/3d-printer-management-releases`) are kept for recovery. Watch disk space, and never delete the current or previous release.

## Scope

**Updated:** application code, web files and Python packages.

**Never touched:**
- Raspberry Pi OS and system packages
- networking and systemd units
- the updater itself

No shell scripts from a ZIP are ever run.
