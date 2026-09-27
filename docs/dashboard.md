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
  - **Buttons:** **Details & camera**, **Printer files** (downloads a listing of the printer's storage), **Pause**, **Resume**, **Light on/off**, **Stop** (asks for confirmation), and **N queued →** (opens that printer's queue).
- **+ Queue a print:** at the top right. See [Print queue](print-queue.md).
- **Demo mode:** a **DEMO MODE** badge shows when no real printers are being controlled.

## Printer details

**Details & camera** opens a dialog for one printer:

| Section | What it shows / does |
|---|---|
| Summary | State, progress, layers, last telemetry time |
| Temperature & speed | Current and target nozzle/bed temperatures, speed profile |
| Filaments | AMS units (humidity, temperature) and each slot's material, colour and remaining %, plus the external spool |
| Live camera | **Start / Stop live view**: about 1 frame per second. See [Printer controls & camera](printer-controls.md#camera). |
| Printer controls | Nozzle/bed/chamber temperature, speed profile, a single fan or all fans, and axis movement. Each change asks for confirmation. See [Printer controls](printer-controls.md). |
| Swapmod A1m | Kit settings for equipped A1 minis. See [Swapmod](swapmod.md). |
| Snapshot | Take a single camera picture |

Close the dialog with ✕. Closing it also stops live view.

## Print queue

- **The queue:** each printer's waiting jobs in order. The first job has **Start next** and **Start ignoring error**, and every waiting job has **↑ ↓ Remove**.
- **Needs review:** jobs whose outcome is uncertain show **Mark finished / failed / cancelled**.
- **History:** finished jobs, each with **Queue again**.
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
| Discord settings | Set the notification channel ID, public commands channel ID and **approved user IDs** (admins without Discord Administrator). **Send test notification** checks the channel. |
| Dashboard access | Change the dashboard password (12+ characters). This signs everyone out. |
| Diagnostics & error logs | Download a diagnostic ZIP and see recent errors with their error IDs. See [Troubleshooting](troubleshooting.md). |
| Send a problem report | Send a description plus diagnostics to the developers as a GitHub issue, and set the report destination. See [Troubleshooting](troubleshooting.md#sending-a-problem-report). |
| GitHub releases | Repository, token, **update channel** (stable / beta / alpha), automatic installation, **Check now**. See [Updates](updates.md). |
| Software update | Upload a release ZIP, review it, install it. See [Updates](updates.md). |
| Quick guide | A short summary of the queue rules and Discord commands |
