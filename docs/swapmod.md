# Swapmod A1m (Swap Systems)

Support for the **[Swapmod A1m STL Edition](https://swap-systems.com/product/swapmod/)** plate changer on the **Bambu A1 mini**. The kit runs a batch of prints back to back, swapping build plates automatically.

It replaces the earlier Infinity Flow integration. When you update, old Infinity Flow settings are turned off and their plate counts cleared. Don't reuse them for a different mechanism. Existing queue jobs are kept.

**The app doesn't generate or change swap G-code.** Batches come from the vendor's **Swaplist** app; the app only tracks them and keeps the checks you have to do in order.

- [Supported hardware](#supported-hardware)
- [1. Enable the kit on a printer](#1-enable-the-kit-on-a-printer)
- [2. Prepare a batch with Swaplist](#2-prepare-a-batch-with-swaplist)
- [3. Queue and approve the batch](#3-queue-and-approve-the-batch)
- [4. Check the starting setup and start](#4-check-the-starting-setup-and-start)
- [How plate counting works](#how-plate-counting-works)
- [Discord commands](#discord-commands)

## Supported hardware

- **Supported:** A1 mini with the Swapmod A1m kit installed.
- **Only shown for A-series printers (A1 / A1 mini).** Other printers (H2D, X1, P1, P2…) don't show the Swapmod settings in the dashboard, aren't offered in `/plateswap` autocomplete, and the server refuses Swapmod on them. The full-size A1 shows the settings, but the Swapmod A1m kit can only be enabled on an A1 mini.
- **How the model is detected:** the printer's `"model"` in config.json (for example `"A1 mini"`). Without one, the serial number (`030…` = A1 mini, `039…` = A1). Without a serial, the printer's name. If an A1 mini doesn't get the Swapmod settings, set its `"model"`.
- **Settings saved earlier** for a printer that isn't A-series are switched off when the service starts, with a note in the Activity feed.
- **Other printers** keep the normal manual queue workflow.

## 1. Enable the kit on a printer

Dashboard: **printer panel → More → Swapmod A1m (Swap Systems)** on that printer.

1. Tick **Kit installed — enable for this printer**. Only do this on printers that actually have the kit.
2. **Kit model:** *A1 mini*.
3. **Blank plates loaded in magazine:** the real number of spare plates.
4. Save, then confirm.

What the toggle does and doesn't do:
- **Hardware and files:** it doesn't change the mechanism, and it doesn't remove swap G-code from files.
- **Turning it off:** blocks any previously approved swap batch from starting.

## 2. Prepare a batch with Swaplist

1. Slice your prints normally.
2. Use the **official Swaplist app** for your purchased edition to convert them into **one batch file** containing the prints and the plate-changing sequence. See the [vendor instructions](https://swap-systems.com/stl/). The STL-edition converter needs your purchaser login; the converted files work offline.
3. Export the **converted, sliced `.gcode.3mf` batch**.

What not to upload:
- **The unsliced kit assembly project.** Queue only the converted batch.
- **Anything from FlowQ.** No FlowQ hub or software is involved.
- **Vendor files:** none are bundled with this project.

## 3. Queue and approve the batch

1. **Queue the batch** as **one job**, like any other print. See [Print queue](print-queue.md). Pick the plate entry that runs the batch, normally plate 1; check this against your export.
2. **Approve it:** on the queued job, choose **Approve Swaplist batch…** and enter the **total number of plates the batch needs**. Confirm that you generated it with Swaplist and checked the plate count, filament/AMS mapping and starting setup.
   - This is **your confirmation**. The app doesn't inspect the batch G-code or count its plates.

## 4. Check the starting setup and start

1. **Set the printer up** by following the vendor's starting instructions, and clear the ejection path.
2. **Record the check:** choose **Swapmod starting setup checked…**, or `/plateswap check`. This only records your inspection; it sends no movement.
   - It deliberately doesn't tell you to mount a plate, because the batch may load its first plate itself.
3. **Start** with the usual **Start next** or `/queuestart`.

The batch then controls its own plate changes:
- **One job:** the app treats the whole batch as a single job with overall progress. It doesn't detect individual plate swaps.
- **Separate batches:** each needs a new starting check and a manual start.

## How plate counting works

- **Reserved on acceptance:** when a batch is accepted, its whole plate count is subtracted from the magazine count in one step.
- **No refunds:** uncertain or failed jobs don't automatically return plates. Inspect and recount the magazine, then update the count.
- **Restarting the service** keeps the kit settings and counts, but clears setup checks and batch approvals.
- **Changing kit settings** also clears approvals, and copied jobs need a fresh approval.

## Discord commands

All Swapmod commands are admin-only and ask for confirmation.

```
/plateswap configure name:Steward enabled:true spares:5     (model defaults to A1 mini)
/plateswap approve job_id:<id> plates:3
/plateswap check name:Steward
/queuestart name:Steward
```

**Not yet tested on physical hardware:** this integration was tested with simulations only, not a real Swapmod kit or a real converted batch. Before a real run, check a sample exported batch: its metadata and which plate entry to select.
