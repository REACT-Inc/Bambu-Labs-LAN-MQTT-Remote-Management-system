# GitHub release integration validation

146 automated tests passed. Added coverage includes stable version ordering, draft/prerelease and missing-asset rejection, checksum and manifest-version mismatches, private asset redirects without credential forwarding, attempted-release persistence, manual retry rules, active/offline/stale printer guards, authentication/CSRF, and token redaction. Both workflow YAML files parsed; shell/JavaScript syntax and final ZIP source syntax passed. The package contains both workflows and .gitignore, stamps version.py consistently, includes all installer files, and excludes live config/auth/token files. GitHub-hosted Actions, repository publication, real private-token downloads, browser visual rendering and Pi deployment were not performed.

# Camera / transfer recovery validation

134 tests passed. Seven new tests cover H2D-specific TLS close selection, unchanged A1 behavior, explicit 426 rejection even after all bytes are locally sent, remote-size mismatch, camera reuse between requests, authenticated JPEG-frame HTTP responses, and credential redaction. Browser JavaScript syntax passed. Actual printer/USB/network behavior and visual browser rendering remain unverified.

# Latest update validation

127 automated tests pass, including 11 ZIP-updater tests and 7 fan/chamber tests. Fan tests cover reported IDs, percentage payloads, automatic-mode restrictions, all-fan selection, legacy A1 behavior, chamber limits, partial failures and correlated firmware rejection. Discord tests cover public replies, the three private workflows and admin/approved-ID gates for fanall and chamber. Updater coverage includes damaged packages, traversal/symlink rejection, required manifests, checksums, unsafe dependency directives, HTTP login/CSRF checks, confirmation, active-printer blocking, maintenance mode, immutable request snapshots, simulated dependency failures, successful installation, failed-health rollback, and interrupted-switch recovery. Worker tests use temporary directories and mock systemd/pip/health; no actual Pi deployment was performed. Bash and JavaScript syntax checks passed.

The current updater interface has not been visually tested in a browser. Earlier browser results below refer to prior versions and do not verify the current MeshCentral or updater screens.

## Earlier validation record

# Validation performed

- 36 automated tests passed using Python 3.12, discord.py 2.7.1, paho-mqtt 2.1.0 and aiohttp 3.14.3.
- All Python files compiled, the installer passed Bash syntax validation, and browser JavaScript passed Node syntax validation.
- Discord registered the printer, queue, notes, reminder, channel archive, assignment, laptop, server and reboot command definitions without starting a real Discord connection.
- Browser workflow passed in headless Chromium: login, printer list, add/start a demo job, pause/resume, confirmed stop, manual review, requeue, completion, details view, mobile width and no JavaScript errors.
- Team browser workflow passed: save team settings, create a shared note, persist a reminder, enroll a laptop, show Pi status, verify disabled reboot by default, mobile width and no JavaScript errors.
- Queue tests covered concurrent submissions, first-in-first-out order, persistence, restart uncertainty, stale telemetry, cancellation during staging, matching print identity and separation of demo/live jobs.
- Access tests covered sessions, wrong passwords, cross-origin/CSRF rejection and preservation of large Discord IDs.
- Team tests covered roster rotation, idempotent assignment occurrences, archive permission restoration, reminder persistence, one-time agent delivery, local command allowlists, offline devices and default-disabled reboot.
- Migration check preserved token/settings/printer literals, retained real printer definitions beneath a temporary demo override, and did not overwrite an existing dashboard password.

Not tested on user equipment: Raspberry Pi systemd/apt installation, live Bambu FTPS/MQTT job startup, actual Discord permissions and delivery, physical cameras, Windows service installation, host reboots, or the user's Tailscale access policy. These integrations must be checked on the actual devices. No live Discord messages or device commands were sent during development.

The browser tests used fake printer data and a local dashboard-only configuration. No real credentials or configuration are bundled in this download.
