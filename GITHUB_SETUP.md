# GitHub setup and automatic Pi updates

This guide takes the current 3D Printer Management source to your own GitHub repository, publishes installable releases, and connects your Pi to those releases. Nothing has been published to your GitHub account yet. Replace `YOUR-USERNAME` below with your GitHub username or organization name.

## 1. What is included

| File | Purpose |
| --- | --- |
| `.github/workflows/tests.yml` | Tests pushes to main and pull requests on Python 3.12 and 3.13 |
| `.github/workflows/release.yml` | A GitHub Actions button that tests, builds and publishes a release |
| `.gitignore` | Excludes runtime credentials, queues, environments and print files |
| `build_release.py` | Builds the ZIP and its internal file-checksum manifest |
| `github_updates.py` | Checks releases, downloads/verifies assets and requests installation |
| `web_updates.py` / `update_worker.py` | Existing installer, backups, restart checks and rollback |
| `version.py` | Version shown in the dashboard; stamped automatically into release ZIPs |

The baseline version in this download is **1.0.0**. Use **v1.0.0** for the first GitHub release, then **v1.0.1**, **v1.0.2**, etc. Earlier date-based ZIP versions precede this GitHub version scheme. The GitHub updater accepts only stable three-part versions; it ignores downgrades and never installs a branch or a raw source archive.

## 2. Create the repository using GitHub Desktop

1. Sign into your GitHub account at https://github.com/.
2. Install GitHub Desktop from https://desktop.github.com/download/ and sign in.
3. Download this management ZIP and extract it somewhere separate from the running Pi installation.
4. In GitHub Desktop select **File → New repository**.
5. Name it **3d-printer-management**. Pick a local folder where you will keep the source.
6. Leave the generated README and license options unset; the project already has documentation and third-party license notices. Create the repository.
7. Select **Repository → Show in Explorer** (Finder on macOS).
8. Open the extracted ZIP's **printer-management** folder. Copy its **contents** into the new repository folder. `main.py` must be at the repository root, not inside another `printer-management` folder.
9. Include `.github` and `.gitignore`. In Finder use Command+Shift+Period to reveal dotfiles if needed. On Windows enable hidden items if necessary. Do not replace or copy a `.git` folder.
10. In GitHub Desktop review the Changes list. Expect Python files, static assets, tests, documentation and the two workflow files. Do not upload your actual Pi `/etc/3d-printer-management` or `/var/lib/3d-printer-management` folders. They contain credentials and live data. `.gitignore` is a safeguard, not a substitute for checking.
11. Enter a commit summary such as **Initial printer management source** and click **Commit to main**. If your branch has a different name, rename it to `main` before proceeding; the supplied release workflow requires main.
12. Click **Publish repository**. Select your account/organization and keep it private if you do not want the source public, then publish. Public repositories also work.
13. Open the repository on GitHub. Confirm that `main.py`, `README.md`, and `.github/workflows/release.yml` are present at their expected paths. A ZIP uploaded as one repository file is not sufficient.

Keep `BAMBU_RESOURCE_LICENSE.txt` and `ERROR_SOURCES.md` with the source and releases. This package does not introduce a new blanket license for every bundled component.

## 3. Publish your first release

1. On your GitHub repository, open **Actions**. Enable Actions if GitHub asks.
2. The **Tests** workflow should run when main is pushed. Open it and check that it passes.
3. Select **Publish release** in the left sidebar.
4. Click **Run workflow**, select branch **main**, and enter **v1.0.0**.
5. Click the green **Run workflow** button.
6. Open the resulting run. It installs dependencies, runs tests, checks shell/JavaScript syntax, builds the ZIP, creates a draft release, uploads both assets, then publishes it as latest.
7. Go to the repository's **Releases** page. The release must contain:
   - `3d-printer-management.zip`
   - `3d-printer-management.zip.sha256`
8. GitHub also shows automatic **Source code (zip)** and **Source code (tar.gz)** downloads. Those are not installer packages. The Pi uses the specifically named management ZIP above.

The workflow uses GitHub's automatically supplied `GITHUB_TOKEN` with repository `contents: write`. You do not need to put a personal token into the workflow or repository secrets. If organization policy blocks Actions or release writing, an organization administrator must allow it. If a run leaves a draft/tag after an upload failure, inspect it and either remove the incomplete release and its tag before retrying or publish a new version; completed tags should not be reused.

## 4. Install the GitHub-enabled version on printer-hotspot

Use the Pi running `3d-printer-management`, normally **printer-hotspot**, not printer-uplink.

If Settings already has **Software update**:

1. Finish active prints and resolve any queue jobs marked Needs review.
2. Upload this supplied ZIP in **Settings & help → Software update**.
3. Review and install it.
4. Wait for the restart, refresh the page, and sign in again.
5. Settings will now include **GitHub releases**.

If Software update has not been installed yet:

1. Copy the ZIP to the Pi's home directory as `3d-printer-management.zip`.
2. Run these commands on printer-hotspot:

   ```bash
   mkdir -p ~/pm-github-setup
   python3 -m zipfile -e ~/3d-printer-management.zip ~/pm-github-setup
   cd ~/pm-github-setup/printer-management
   sudo bash update.sh
   ```

3. Refresh and sign in to the existing dashboard. `update.sh` preserves configuration, saved passwords, queues and uploads. A brand-new Pi instead uses `sudo bash install.sh`; see README.md for initial configuration.

## 5. Connect the Pi to your repository

1. In **Settings & help → GitHub releases**, set Repository to:

   ```text
   YOUR-USERNAME/3d-printer-management
   ```

   Use owner/repository only, without `https://github.com/`.
2. For a public repository you can leave the token blank. A token is optional to get authenticated API limits.
3. For a private repository create a token as described in the next section and paste it into the dashboard token field.
4. Leave automatic installation unchecked initially. Click **Save GitHub settings**.
5. Click **Check now**. If the installed version and first release are both 1.0.0, **Already up to date** is the expected result. It proves the release lookup works; it is not an error.
6. When a newer release exists, **Install release…** downloads and checks it after you confirm.
7. To enable unattended installation, check **Automatically install newer releases when all printers are idle**, save and confirm that you trust that repository's releases.

Anyone with your shared dashboard password has management access, including update settings. The repository's release publishers become trusted software suppliers for your Pi. Checksums catch damaged/mismatched downloads; they are not an independent publisher signature.

## 6. Token for a private repository

1. In GitHub open your account **Settings → Developer settings → Personal access tokens → Fine-grained tokens**.
2. Generate a new token named **Printer Pi release reader**.
3. Set an expiration and a reminder to renew it before it expires.
4. Choose the resource owner that owns the repository.
5. Under Repository access select **Only select repositories**, then **3d-printer-management**.
6. Under Repository permissions set **Contents: Read-only**. Metadata read access is included by GitHub. The Pi does not need write, Actions or administration access.
7. Generate and copy the token into the Pi dashboard. Do not paste it into Python source, Git commands, README files or GitHub issues.
8. If your organization requires approval, have the token approved before testing.

The token is stored on the Pi in `/var/lib/3d-printer-management/github-updates.json` with file permissions 600 and is never returned to the browser. Blank means keep the saved token; **Clear saved token** removes it. Changing repositories while retaining a token requires explicit token replacement or clearing. When GitHub redirects an asset download, the token is not forwarded to the asset host.

## 7. Publish every future update

1. Edit the source in your GitHub Desktop repository, or copy updated source files into it. Keep `.github`, `.gitignore`, and your repository's `.git` metadata.
2. Review changes, commit with a useful summary and **Push origin**.
3. Wait for Tests to pass.
4. Run **Actions → Publish release → Run workflow → main** with a higher version, for example **v1.0.1**.
5. The workflow stamps that version into the release package automatically. You do not need to edit `version.py` for every release build. A plain source checkout still displays its source version; deploy the built release asset for accurate version reporting.
6. Once the release is published, the Pi discovers it at its next check. Use **Check now** to refresh discovery immediately; the automatic scheduler will consider installation within its next five-minute cycle. Use **Install release…** for an immediate confirmed attempt.

Editing files or pushing commits alone does not deploy anything. Only publishing a newer stable release with both assets makes an update eligible. Do not mark test builds as latest stable releases. Prereleases/drafts are never auto-installed.

## 8. Automatic update behavior

- Checks start about one minute after management starts, then successful release discovery is refreshed hourly. Errors and waiting-for-idle conditions are revisited every five minutes.
- Auto-installation requires every configured real printer to be connected with a report less than 90 seconds old and an idle/finished/failed state. RUNNING, PAUSE, PREPARE, unknown/offline printers and active/unresolved queue jobs block it. Check configuration if a retired printer keeps updates waiting.
- The app rechecks printer state after downloading, before submitting installation.
- Download is bounded to 32 MiB, ZIP and SHA-256 assets must match, the manifest is validated, and its version must match the release tag.
- Installation uses the existing fixed system updater. It prepares a separate Python environment, backs up config and mutable data, switches application releases and checks local dashboard startup. Failed startup triggers rollback.
- Config, queues, Pi-side print uploads and credentials are preserved. Network settings and Raspberry Pi OS are not upgraded. Shell scripts inside downloaded ZIPs are not run. A future change to the privileged updater itself still requires a manual installer update.
- New management actions pause while installation is pending. Avoid starting prints from another slicer/panel during the update. The app cannot lock independent printer controllers.
- An attempted release is recorded before installation. After failure/rollback, the same version is not retried automatically. Inspect Software update status and logs, then use a confirmed manual retry or publish a higher version. Download/validation failures before installation can be retried on a later check.
- Auto-update settings and attempted-version history survive restarts. Unchecking automatic installation stops future automatic attempts; it cannot cancel an installation already handed to the worker.
- Backups and old releases remain on disk for recovery. Monitor free space; there is no automatic backup deletion in this release. At least 1 GiB free on the application disk and internet access for Python packages are required.

## 9. Troubleshooting and recovery

**No Run workflow button:** confirm `.github/workflows/release.yml` is committed on main, Actions are enabled, and you have repository write access.

**404 from GitHub:** verify owner/repository spelling, that at least one release is published, and private-repository token access. GitHub can return 404 for repositories you cannot access.

**401 or 403:** check token expiration, organization approval, repository permissions, rate limits, and organization Actions policy where applicable.

**Missing ZIP/checksum or invalid version:** use the supplied Publish release workflow and `vMAJOR.MINOR.PATCH`. GitHub's generated source ZIP is not a management update.

**Already up to date:** installed version is at least as new as the latest release. Publish a larger version for a new update; changing an existing release asset is not an update mechanism.

**Waiting for idle:** finish/reconcile managed jobs, confirm all configured printers are online, and allow fresh telemetry to arrive.

**Install failed or rolled back:** the dashboard's Software update panel shows the worker result. On printer-hotspot run:

```bash
sudo systemctl status pm-web-update 3d-printer-management --no-pager
sudo journalctl -u pm-web-update -n 80 --no-pager
sudo journalctl -u 3d-printer-management -n 80 --no-pager
```

See WEB_UPDATES.md for recovery details. A healthy local dashboard does not prove every camera, Discord permission or printer feature works. Test the features you changed after each release. Publishing a corrected higher version is the supported path back to known-good behavior; GitHub auto-update does not downgrade versions.

## Optional: use Git commands instead of GitHub Desktop

Use these only in the clean extracted source folder, not `/opt/3d-printer-management`. Create an empty repository called `3d-printer-management` on GitHub first, without generating files. Authenticate through your Git credential manager or `gh auth login`; do not place tokens in the URL.

```bash
git init -b main
git add .
git status
# Inspect the staged file list before committing.
git commit -m "Initial printer management source"
git remote add origin https://github.com/YOUR-USERNAME/3d-printer-management.git
git push -u origin main
```

If Git asks for your identity, set `git config user.name "Your Name"` and `git config user.email "Your GitHub commit email"` in this repository, then repeat the commit. Continue with step 3 above to publish a release.

## Official references

- GitHub Desktop setup: https://docs.github.com/en/desktop/overview/creating-your-first-repository-using-github-desktop
- Running a workflow: https://docs.github.com/actions/managing-workflow-runs/manually-running-a-workflow
- Release assets and downloads: https://docs.github.com/en/rest/releases/assets
- Token setup: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
- Release creation: https://cli.github.com/manual/gh_release_create

This integration is locally tested with mocked GitHub responses and worker calls. Publishing in your account, private-token access, GitHub-hosted CI and deployment on your Pi require verification after setup.
