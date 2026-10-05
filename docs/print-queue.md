# Print queue

Each printer has its own first-in, first-out queue. The queue is shared by the dashboard and Discord: a job added in one appears in the other.

**The queue never starts a print on its own.** Someone must confirm the build plate is clear and start the next job.

- [What you can queue](#what-you-can-queue)
- [Adding a job](#adding-a-job)
- [Starting a job](#starting-a-job)
- [Job states](#job-states)
- [When a job needs review](#when-a-job-needs-review)
- [Reordering, removing and reprinting](#reordering-removing-and-reprinting)
- [Limits and things to know](#limits-and-things-to-know)

## What you can queue

- **A sliced `.3mf` from Bambu Studio or OrcaSlicer**, uploaded from your computer. It must contain the G-code for the selected plate (`Metadata/plate_N.gcode`) and be sliced for that exact printer, nozzle and material. The dashboard checks this as soon as you pick the file.
- **A file already on the printer's storage**, given as a relative path ending in `.3mf`, for example `cache/part.gcode.3mf`. Use `/file list` or **Printer files** to find paths.

What you can't queue:
- **STL files and unsliced projects.** The app doesn't slice.
- **Plain `.gcode` files.** They're listed by the file browser but can't be started through the queue.

## Adding a job

**Dashboard:** **+ Queue a print**, then choose:

| Field | Meaning |
|---|---|
| Printer | The printer this file was sliced for |
| Job name | 1–120 characters, shown in the queue and notifications |
| File source | Upload a sliced `.3mf` (up to 256 MiB), or a path already on the printer |
| Plate | **Upload:** pick a plate card (see below). **Path on the printer:** type the plate number, 1–100. |
| Bed type | Textured PEI, Smooth/high temperature, Cool plate, or Engineering plate |
| Use AMS / AMS mapping | Off = external spool. On = a comma-separated list with one entry per filament in the project: the AMS tray (`0` = AMS 1 slot 1, `1` = AMS 1 slot 2, … `4` = AMS 2 slot 1) or `-1` for a filament this plate doesn't use. For uploads it's filled in for you (see below). |

**What happens when you pick a file (dashboard):**
- **It's checked straight away:** the file uploads and is read right away, before you submit anything.
  - **Unsliced project:** you're told to slice it and use **File → Export → Export plate sliced file** in Bambu Studio.
  - **Wrong model:** a file sliced for a different printer model (say an A1 mini file for the H2D) is flagged, and adding it is refused.
- **Plate picker:** each plate in the file is shown as a card with its thumbnail, name, print time, weight and filament colours. Plates that weren't sliced are greyed out. A file with one sliced plate picks it for you.
- **AMS suggestion:** for the chosen plate and printer, each filament is matched to a loaded AMS slot of the same material with the closest colour. **Use AMS** and the mapping are filled in, and the matches are listed, for example "Filament 2 (PETG) → AMS 1 slot 3". If something has no loaded slot of that material, the mapping is left for you. You can always edit it.

**Print now (dashboard):** **Print now** instead of **Add to shared queue** prints straight away, without waiting in the queue.
- **Same checks as Start next:** it asks the same "plate is clear" confirmation, and the printer must be idle with no active job.
- **Jumps the queue:** it starts ahead of any waiting jobs for that printer.
- **If it can't start:** the printer is busy, offline or reports an error. Then nothing is added, or the job is cancelled at once, so it never sits in the queue.

**Discord:** `/queueadd` with an attached `.3mf` (`file:`) **or** a printer path (`remote:`), plus the same options. Discord may limit attachment size below 256 MiB.

How an uploaded file is used:
1. It's stored on the Pi.
2. When the job starts, it's copied to the printer over FTPS.
3. The copy is checked (the transfer must complete and the remote file size must match).
4. Only then is the print command sent over MQTT.

## Starting a job

Only the **first waiting job** of a printer can start. Before you start it:

1. **Clear the build plate** and check the right material is loaded.
2. **Check the file** was sliced for this printer.
3. **Start it:** dashboard **Start next**, or Discord `/queuestart` (admins). Then confirm.

The start is refused if:
- the printer is offline, or hasn't reported in the last 90 seconds
- the printer isn't IDLE or FINISH
- the printer reports an error
- the printer already has an active or unresolved job
- a software update is in progress

When the printer reports FAILED or an error, the dashboard's **Start next** (and **Print now**) button says so and asks whether to start anyway. Confirming allows that one attempt despite the error; `/queueforce` does the same in Discord.
- It **doesn't** clear the error on the printer or bypass firmware protections.
- Use it only after inspecting the printer.
- Every use is recorded in Activity.

Printer readiness is checked twice: before staging, and again right before sending. The app still can't reserve the printer against someone starting a print from Bambu Studio or the printer's screen at the same moment, so avoid starting prints elsewhere while a job is staging.

## Job states

| State | Meaning |
|---|---|
| `queued` | Waiting in line |
| `staging` | Being uploaded to the printer / about to be sent |
| `awaiting_start` | Print command sent; waiting for the printer to report it running **this** file |
| `printing` | The printer confirmed it's running this job |
| `paused` | The printer paused; the queue holds |
| `finished` / `failed` / `cancelled` | Done. Shown in history. |
| `needs_review` | The outcome is uncertain. A person must check the printer (see below). |

A job only becomes `printing` when the printer reports it's running a matching file. A finish or failure report for a *different* file doesn't count, and neither does a successful upload on its own.

## When a job needs review

A job becomes `needs_review` when the app can't tell what happened:
- **No confirmation within 2 minutes:** the printer didn't report the matching print after the command was sent.
- **Restart:** the service restarted while the job was active.
- **Unexpected report:** the printer reported a different job, or went idle without a result.
- **Stop:** someone pressed stop after the print command may already have been sent.

**What to do:**
1. **Look at the printer.** Is it printing, finished, or did it never start?
2. **Record the real outcome.** On the dashboard use **Mark finished / failed / cancelled**; in Discord use `/queuemanage action:resolve_…`. This only records a result: it sends nothing to the printer.
3. **Don't mark a running print finished** just to free the queue.

Until it's resolved, no other job can start on that printer. **Uncertain starts are never retried automatically.**

## Reordering, removing and reprinting

- **Reorder:** ↑ / ↓ on the dashboard, or `/queuemanage action:up|down`. Only waiting jobs can move.
- **Remove:** **Remove**, or `/queuemanage action:remove`. The job is marked cancelled.
- **Stop during staging:** cancels the pending submission.
- **Reprint:**
  - On the dashboard, **Queue again** on any job in history (finished, failed or cancelled) copies it back into the queue with the same file and options.
  - **Print on another printer…** (dashboard only) copies a history job into a **different printer's** queue.
    - **Which printers:** only printers of the model the file was sliced for. The app reads the model from the `.3mf`, and the printer's model from its `"model"` in config.json or its serial number. Other models are greyed out with "Re-slice it for the … in Bambu Studio". If either model is unknown, you have to tick that you checked the file suits that printer.
    - **What carries over:** plate and bed type. Choose the AMS mapping again for the new printer's trays (the old one is pre-filled).
    - **Starting:** the new job waits in that printer's queue for the normal **Start next** confirmation. The history entry stays as it was, and the Activity feed records the move.
    - **Files on the printer's storage** (`remote:` jobs from `/queueadd`): the app copies the file from the original printer to the Pi over FTPS when you send it, then uploads it to the new printer when the job starts. The original printer must be on and reachable. Which printers are offered is based on the original printer's model, and the copied file is checked again before it's queued.
  - In Discord, `/reprint` does the same for the most recent finished job.
  - Either way the copy waits for a normal start. The app doesn't guess a file path from the printer's "last job" name.

## Limits and things to know

- **Printer models:** H2D-specific features such as tool switching aren't modelled. Jobs use the printer's default nozzle behaviour.
- **Storage cleanup:** uploaded files and history stay on the Pi. There's no automatic cleanup yet.
- **Demo mode:** demo jobs run a fake 22-second print and can't be started after you switch to live mode.
- **Swapmod batches:** A1 minis with a Swapmod kit need extra approval steps. See [Swapmod](swapmod.md).
- **Upload errors:** if an upload fails, the queue shows the step, the bytes sent, and the FTP error. See [Troubleshooting](troubleshooting.md#h2d-upload-fails-with-426).
