# Dashboard software updates

Install this release once from the extracted ZIP with `sudo bash update.sh`.
New installations use `sudo bash install.sh`. Thereafter open **Settings & help → Software update**, upload a management release ZIP, review its version and confirm **Install reviewed update**. Finish or resolve active queue jobs and wait for printers to finish first. Reconnect and sign in after restart to see the outcome.

Only authenticated dashboard administrators can upload/install. The existing dashboard password grants administrative access. Upload only trusted releases; installed application code has access to printer, Discord and MeshCentral credentials. Manifest checks detect corruption; they are not a publisher signature. Old ZIPs without `update-manifest.json` are rejected.

The fixed root-owned worker validates the ZIP again, prepares a separate application release and Python environment, stops management, backs up configuration and mutable data, switches releases and checks the new dashboard several times. Failed startup triggers restoration of the previous code, config and data. Printer-uploaded files are kept in place. Queue jobs are not automatically restarted. Health checks verify local dashboard startup, not Discord connectivity, camera availability or every feature.

Preparation needs internet access to install Python dependencies and at least 1 GiB free on the application disk. Dependency installation runs as printermanager. A failed dependency install keeps the existing release. This updates application code, web assets and Python dependencies; it does not upgrade Raspberry Pi OS, networking, system packages, or execute shell scripts from ZIPs. The privileged updater itself requires a manual installer update if it changes.

Updates pause new dashboard changes and Discord commands. Avoid operating printers independently during installation. Backups remain root-only in `/var/lib/pm-updater/backups`; releases remain in `/opt/3d-printer-management-releases`. They are retained for recovery, so monitor disk space. Do not remove the current or previous release. An interrupted switch is recovered by the updater service at boot. If the web page is unavailable, use:

```bash
sudo systemctl status pm-web-update 3d-printer-management --no-pager
sudo journalctl -u pm-web-update -n 60 --no-pager
```

If status says recovery is required, inspect the log and run `sudo systemctl restart pm-web-update` after addressing the reported problem. Do not delete its journal to bypass recovery.

## Building a release ZIP

Run `python3 build_release.py VERSION /absolute/output.zip` in the source folder. Use a distinct version containing only letters, digits, dots, underscores or hyphens. The builder generates the manifest and ZIP with the required `printer-management/` root. It excludes caches, local config, environments and uploaded files. Tests and installer scripts may be included for manual installation, but the web updater only extracts manifest-listed application files.


GitHub releases are now supported in Settings. See GITHUB_SETUP.md for repository setup, publishing and opt-in automatic installation. Manual ZIP upload remains available.
