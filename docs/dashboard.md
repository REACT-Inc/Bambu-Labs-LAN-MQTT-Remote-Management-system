# Web dashboard

The dashboard is served by the Pi at `http://<address>:8080`. It's protected by a single shared password: **anyone who signs in has full management access**, so give the password only to people who should manage printers. Sessions last 12 hours.

The page refreshes printer data every 5 seconds and works on phones as well as desktops.

- [Overview](#overview)
- [Printer details](#printer-details)
- [Print queue](#print-queue)
- [Activity](#activity)
- [Team tools](#team-tools)
- [Laptops](#laptops)
- [Server](#server)
- [Settings & help](#settings--help)

## Overview

- **Summary tiles:** printers online, printing now, jobs waiting, and items needing attention (offline printers, printer errors, jobs needing review).
- **A card per printer:**
  - **Status:** state (IDLE, RUNNING, PAUSE, FAILED, OFFLINE…), current file, progress, time left, and nozzle/bed temperatures.
  - **Errors:** any printer error, with the official Bambu description.
  - **Filaments:** small colour swatches for the loaded AMS slots.
  - **Camera:** printers with a camera show a **still snapshot** with the time it was taken. While the dashboard is open, the Pi takes one snapshot at a time, one printer after another, so each card updates about every 30–40 seconds. A camera that doesn't answer shows *Camera unavailable* and is retried a couple of minutes later.
  - **Buttons:** only the ones that apply right now: **Pause** and **Stop** (both ask for confirmation), **Resume**, the chamber **light** 💡, and **N queued →** (opens that printer's queue).
  - **Waiting for the printer:** a button you've clicked **pulses purple** until the printer reports the result, then shows the real state. For example, the light goes solid amber when it's on. Purple is only used for "waiting". If the printer doesn't report the change in time (10 s for the light, 20 s for other controls), the button goes back and a message says so.
  - **Opening a printer:** click anywhere else on the card (or focus it and press Enter or Space).
- **+ Queue a print:** at the top right. Pick a file and its plates appear as cards (thumbnail, time, weight, colours), the AMS mapping is suggested from the printer's loaded trays, and **Print now** starts it straight away instead of queuing. See [Print queue](print-queue.md#adding-a-job).
- **Demo mode:** a **DEMO MODE** badge shows when no real printers are being controlled.

## Printer details

Clicking a printer card opens its **printer panel**. It slides in from the right on a computer, or up from the bottom on a phone.

| Part | What it shows / does |
|---|---|
| Header | Name, state, a progress ring with layers and time left, and **Pause / Resume / Stop / light** |
| Camera | Shows the latest still snapshot straight away. **▶** starts live view (about 1 frame per second) and **Stop live view** stops it. See [Printer controls & camera](printer-controls.md#camera). |
| Temperature | Tiles for nozzle, bed and chamber. Tap a tile to type a new target. |
| Print speed | Silent, Standard, Sport, Ludicrous |
| Fans | A slider per fan, plus all fans. The fan is set when you let go of the slider. It shows *Setting…* until the printer reports the new speed. |
| Move axes | **⌂ Home** (homes X, Y and Z while the printer is idle), and a movement pad locked until you tick the safety checkbox. See [Printer controls](printer-controls.md). |
| Nozzle | The fitted nozzle's diameter and type, which you can change after swapping the nozzle. See [Printer controls](printer-controls.md#nozzle-diameter-and-type). |
| Filament | AMS units (humidity, temperature) and each slot's material, colour and remaining %, plus the external spool. **Click a slot** to change its material or colour. See [Printer controls](printer-controls.md#filament-material-and-colour-per-slot). |
| More: files, snapshot, Swapmod | Download the printer's file listing, take a single camera snapshot, and Swapmod A1m kit settings, shown for A-series printers only (see [Swapmod](swapmod.md)) |

**Only Pause and Stop ask for confirmation.** Every control, including temperature tiles, speed, fan rows and movement buttons, pulses purple while it waits for the printer. Temperatures, speed, fans, light and resume apply straight away. The server still enforces the limits and safety checks (see [Printer controls](printer-controls.md)). Movement stays locked until you tick the safety checkbox.

**Closing:** click outside the panel, press **Esc**, or use ✕. Closing it also stops live view. Every other popup closes the same way. Clicking outside a confirmation only cancels that confirmation.

## Print queue

- **The queue:** each printer's waiting jobs in order. The first job has **Start next** and **Start ignoring error**, and every waiting job has **↑ ↓ Remove**.
- **Needs review:** jobs whose outcome is uncertain show **Mark finished / failed / cancelled**.
- **History:** finished jobs, each with **Queue again**, and **Print on another printer…** to send it to a different printer of the same model (see [Print queue](print-queue.md)).
- **Filter:** a printer filter at the top.

Queue rules and job states are explained in [Print queue](print-queue.md).

## Activity

A feed of recent events, newest first:
- printer state changes and errors
- queue changes
- controls submitted
- printer rejections
- renames
- DMs sent (who and to whom, not the text)
- updates

The last 2000 events are kept, and the feed shows the latest 100.

## Team tools

Settings and records shared with Discord. See [Team tools](team-tools.md).

- **Practice & report assignments:**
  - schedule settings: time zone, days, time, channel, role and roster
  - team links used by `/ftc`, `/website` and `/management`
  - **Assign now**, and the assignment history
- **Meeting attendance:** who is attending, not attending or hasn't replied for the next six meetings (per server), with reasons. You can set or clear someone's reply by user ID.
- **Remember this:** shared notes per server.
- **Reminders:** schedule a Discord DM reminder for any user ID, and see delivery status.
- **Channel archive:** archive and restore text channels.

## Laptops

MeshCentral connection settings, the matching laptops with their online state, a button to run approved commands, and command results. See [LAPTOPS.md](../laptopManagement_Intergration/LAPTOPS.md).

## Server

- **Status:** Pi uptime, free disk space, CPU temperature and laptops online.
- **Reboot Pi…:** only works if reboot was enabled at install time, and not while queue jobs are active. See [Installation](installation.md#optional-allow-rebooting-the-pi-from-discord-or-the-dashboard).

## Settings & help

| Panel | Use it to |
|---|---|
| Discord settings | Set the notification channel ID, the **commands channel ID** (replies are public there and private in every other channel) and **approved user IDs** (admins without Discord Administrator). **Send test notification** checks the channel. |
| Discord command permissions | Choose who can use each Discord command: Everyone, Allowed roles + admins (with the **Allowed role IDs** list), Admins only, or Off. Commands marked 🔒 can only be admin-only or off. Opening an admin command to everyone asks for confirmation, and **Reset all to defaults** restores the defaults. See [Commands and permissions](commands.md). |
| Dashboard access | Change the dashboard password (12+ characters). This signs everyone out. |
| Diagnostics & error logs | Download a diagnostic ZIP and see recent errors with their error IDs. See [Troubleshooting](troubleshooting.md). |
| Send a problem report | Send a description plus diagnostics to the developers as a GitHub issue, and set the report destination. See [Troubleshooting](troubleshooting.md#sending-a-problem-report). |
| GitHub releases | Repository, token, **update channel** (stable / beta / alpha), automatic installation, **Check now**. See [Updates](updates.md). |
| Software update | Upload a release ZIP, review it, install it. See [Updates](updates.md). |
| Quick guide | A short summary of the queue rules and Discord commands |
