# Desktop app for Windows laptops

**3D Printer Management for Windows** puts the dashboard in its own window on a laptop, with a tray icon that lists the printers and pops up the dashboard's notifications: prints finished or failed, printer errors, AI alerts.

- **It's the same dashboard:** everything you can do in a browser works in the app, after signing in with the dashboard password.
- **The Pi still does everything:** the app talks only to your Pi, never to the printers directly. Nothing changes on the Pi, and any number of laptops can use the app.
- **No password is stored:** you sign in on the dashboard itself, and the app keeps that sign-in for the dashboard's 12-hour session.

Contents:

- [Download and start](#download-and-start)
- [Connect to your Pi](#connect-to-your-pi)
- [The window](#the-window)
- [The tray icon](#the-tray-icon)
- [Notifications](#notifications)
- [Start with Windows](#start-with-windows)
- [Updating and uninstalling](#updating-and-uninstalling)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [For developers](#for-developers)

## Download and start

1. **Download** `3d-printer-management-desktop.exe` from the same [GitHub release](https://github.com/REACT-Inc/Bambu-Labs-LAN-MQTT-Remote-Management-system/releases) as your Pi's version. The tray needs the Pi on 1.7.8 or newer; the window works with any version.
2. **Put it somewhere permanent,** for example your Documents folder. There's nothing to install: it's one file.
3. **Double-click it.** The first start takes a few seconds while it unpacks itself.

Requirements:
- **Windows 10 or 11** (64-bit).
- **The Microsoft Edge WebView2 Runtime,** a free part of Windows from Microsoft. Windows 11 and up-to-date Windows 10 already have it. If it's missing, the app says so and opens the download page.

**"Windows protected your PC":** the .exe isn't code-signed, so Windows SmartScreen may warn the first time. Choose **More info → Run anyway**. To make sure the download is genuine, compare its checksum with the `.sha256` file from the same release:

```powershell
Get-FileHash .\3d-printer-management-desktop.exe -Algorithm SHA256
```

On school or work laptops, IT may block apps that aren't signed; ask them to allow it.

## Connect to your Pi

The first time, the app asks for the Pi's address:

- **Type the dashboard address** the installer showed, the same one you'd open in a browser, for example `192.168.1.50`, or the Pi's Tailscale address such as `100.101.102.103`. Port 8080 is added for you.
- **Or press Find it for me.** It looks for dashboards on this laptop's own networks (for example 192.168.1.1–254), on its tailnet if Tailscale is installed on the laptop, and on the laptop itself. Pick yours from the list.

The laptop must be able to reach the Pi, the same as a browser: on the same network, or signed in to Tailscale. Then **sign in** with the dashboard password.

- **Staying signed in:** the sign-in is kept when you close or restart the app, until the dashboard's 12-hour session ends, the dashboard's password changes, or the Pi's app restarts (after an update or **Restart now**). Then the dashboard asks you to sign in again.
- **If the Pi can't be reached when the app starts** (for example while Wi-Fi is still connecting), the app keeps trying every 15 seconds.
- **Another Pi:** tray icon → **Change Pi address…** The app connects to one Pi at a time.

## The window

The window is the [web dashboard](dashboard.md), so the guides for the dashboard apply as they are.

- **Downloads,** such as diagnostic reports, ask where to save the file.
- **Links to other websites** open in your usual browser.
- **Zoom** with Ctrl + mouse wheel.
- **Closing the window** keeps the app running in the tray. Open it again from the tray icon, or by starting the app again.

## The tray icon

The app's **3D** icon sits in the notification area at the bottom right of the taskbar. If you can't see it, click the **^** arrow next to the clock; you can drag it onto the taskbar from there.

- **Click it** to open the window.
- **Hover over it** for a summary, for example *2 printing · 1 ready*.
- **Its dot** works like the dashboard's [status icon](dashboard.md#status-icon): none when all is well, blue while something is working, yellow when something needs a look, red when something failed. Grey means the app isn't connected: not signed in, or the Pi can't be reached.

**Right-click it** for the menu:

| Item | What it does |
|---|---|
| **Open 3D Printer Management** | Opens the window |
| One line per printer | For example *H2D — Printing 45% · 1 h 20 min left*, *A1 — Paused at 12%* or *A1 mini — Error: …*. Or why there's nothing to show: not signed in, or the Pi can't be reached. Click a line to open the window. |
| **Notifications** | Which pop-ups to show. See [Notifications](#notifications). |
| **Start with Windows** | See [below](#start-with-windows) |
| **Change Pi address…** | Connects to another address |
| **Reload the dashboard** | Loads the dashboard again |
| **Help** | Opens this guide |
| *Desktop app 1.7.8 · Pi 1.7.8* | The two versions |
| **Quit** | Closes the app completely |

The tray asks the Pi for a small summary every 15 seconds, using the window's sign-in. It doesn't count as someone looking at the dashboard, so it doesn't keep the printers' cameras busy the way an open dashboard does.

## Notifications

The pop-ups are the dashboard's own notifications: the list under its status icon, which Discord and the other [alerts](dashboard.md#other-alerts) get too. For example a print finished or failed, a printer error, a queue job that needs review, an AI alert or an automatic reprint.

Choose which ones in the tray menu under **Notifications**:

| Choice | Pops up |
|---|---|
| **Prints, problems and AI alerts** (default) | Everything except the progress updates every 10% |
| **Everything, also progress every 10%** | Every notification |
| **Only problems** | Errors and warnings |
| **Off** | Nothing (the tray still shows the printers) |

- **Only new ones:** notifications from before the app first connected aren't replayed.
- **Several at once,** for example when the laptop wakes from sleep, become one pop-up listing the newest three.
- **When the sign-in ends,** one pop-up says so. Notifications pause until you sign in again.
- **Focus assist** (*Do not disturb*) in Windows can hide pop-ups.

## Start with Windows

Tick **Start with Windows** in the tray menu to start the app in the tray each time you sign in to Windows, so notifications arrive without opening anything. It's a setting for your Windows account only; no administrator rights are needed.

If you move the .exe, start it once from its new place: Start with Windows follows it.

## Updating and uninstalling

**Updating:** download the .exe from the newer release, **Quit** the running app (tray menu), and replace the old file with the new one. The address, choices and sign-in are kept. The app doesn't update itself.

**Uninstalling:**
1. Untick **Start with Windows**, then **Quit**.
2. Delete the .exe.
3. To remove its settings and sign-in too, delete these two folders (paste each into File Explorer's address bar):
   - `%APPDATA%\3D Printer Management`: the Pi's address and your choices
   - `%LOCALAPPDATA%\3D Printer Management`: the window's sign-in, its cache and the app's log

## Troubleshooting

| What you see | What to do |
|---|---|
| **"Nothing answered at …"** | The laptop can't reach the Pi. Check it's on the same network as the Pi, or signed in to Tailscale, and that the Pi is on. Try the same address in a browser. |
| **"Something answered at …, but it isn't the 3D Printer Management dashboard"** | Check the address and port. The dashboard uses port 8080 unless `port` in `config.json` was changed. |
| **Find it for me finds nothing** | Type the address instead. The search covers addresses like the laptop's own (for example 192.168.1.1–254); it can't see a Pi on a larger or different network, or past a guest Wi-Fi that keeps devices apart. |
| **Tray: "Not signed in"** | Open the app and sign in. |
| **Tray: "Can't reach the Pi"** | As for "Nothing answered" above. The tray recovers by itself once the Pi can be reached. |
| **Tray: "Update the Pi to version 1.7.8 or newer…"** | The window works; the tray's summary needs the newer Pi. [Update the Pi](updates.md). |
| **No pop-ups** | Check **Notifications** in the tray menu, and that Windows notifications and Focus assist allow them. |
| **"needs the Microsoft Edge WebView2 Runtime"** | Install it from the page the app opens, then start the app again. |

The app's log is `%LOCALAPPDATA%\3D Printer Management\desktop.log`. It never contains the password or the sign-in.

## Security

- **No password is stored.** The app keeps the dashboard's sign-in like a browser does, in the app's own web data in your Windows account. It ends when the dashboard's session does.
- **The same connection as a browser:** plain HTTP to port 8080, so the same advice applies: use Tailscale or a trusted network ([network access](installation.md#choose-how-youll-reach-the-dashboard)).
- **It connects only to the Pi you chose.** **Find it for me** only checks addresses on the laptop's own networks and tailnet, and only when you press it. It opens GitHub (Help) and Microsoft's WebView2 download page in your browser when you ask.

## For developers

The app is in `desktop/`:

| File | What it is |
|---|---|
| `desktop_app.py` | The app: the window (pywebview with Edge WebView2) and the tray icon (pystray) |
| `pi_client.py` | Talking to the Pi: addresses, the health check, Find it for me, the tray's summary (`GET /api/desktop`) |
| `desktop_state.py` | Settings, which notifications pop up, the tray's text, Start with Windows, one copy at a time |
| `connect.html` | The Connect page |
| `icon.py` | The icon, drawn with Pillow |
| `build.py` | Builds the .exe with PyInstaller |
| `ci/demo_pi.py`, `ci/smoke_test.py` | The end-to-end test against a demo Pi |

- **Run from source:** `pip install -r desktop/requirements.txt`, then `python desktop/desktop_app.py`. On Linux, also `pip install "pywebview[qt]"`. This is experimental: Linux trays can't show pop-ups, and after a restart the tray needs a fresh sign-in.
- **Build the .exe** on Windows: `pip install -r desktop/requirements-build.txt`, then `python desktop/build.py v1.7.8-beta.1`. The result is `dist\3d-printer-management-desktop.exe`.
- **Tests:** `tests/test_desktop_app.py` runs with the other tests. The **Desktop app** workflow (`.github/workflows/desktop.yml`) builds the .exe on Windows for pull requests and runs `desktop/ci/smoke_test.py` against `desktop/ci/demo_pi.py`, the real dashboard with demo printers: connect (with Find it for me), sign in, the tray's printers and a notification, closing to the tray, starting again, quitting, and staying signed in. Its screenshots and log are kept as the run's `desktop-app-test` artifact.
- **Releases:** the **Publish release** workflow builds and tests the .exe for the release's version and attaches it, with its `.sha256` file, next to the Pi's ZIP.
