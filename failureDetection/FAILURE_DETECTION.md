# AI print-failure detection (Raspberry Pi 5 AI HAT)

The Pi can watch the printer cameras with a Raspberry Pi **AI HAT+ / AI Kit** (Hailo-8L or Hailo-8) and warn you, or pause the print, when a print looks like it has failed (spaghetti, a print knocked off the bed, a blob on the nozzle). It's **off** until you turn it on in `config.json`.

- [How it decides](#how-it-decides)
- [What happens on a failure](#what-happens-on-a-failure)
- [Setting up the AI HAT](#setting-up-the-ai-hat)
- [The model](#the-model)
- [config.json](#configjson)
- [Dashboard and Discord](#dashboard-and-discord)
- [Troubleshooting](#troubleshooting)

## How it decides

One bad-looking picture is never enough. While a printer reports **RUNNING**, the Pi takes one camera still every `interval` seconds (30 by default). It uses the cheap single-still path and reuses a recent dashboard still when there is one, so it doesn't add camera load. The AI HAT scores each still, and the last `window` scores (10 by default) are kept. A print is only called a failure when **all** of these hold:

| Rule | Default |
|---|---|
| At least `needed` of the last `window` frames score at or above `threshold` | 6 of 10, at 0.6 |
| The failing frames span at least `min_minutes` | 4 minutes |
| The newest frame is still failing | |
| The print has been running for at least `warm_up` minutes, because heat-up, purge lines and the first layer can look odd | 3 minutes |

How odd frames are handled:

- **Duplicate frames:** a frame identical to the previous one (a frozen camera, or a reused still) is skipped and isn't counted twice.
- **Camera gaps:** if no frame arrives for more than 4 × `interval`, the evidence starts again.
- **Suspect frames:** two or more failing frames that don't yet meet the rules show **suspect** on the dashboard. Nothing is sent.

With the defaults, a real failure is reported about 4–5 minutes after it becomes visible. Raise `min_minutes` or `needed` if you get false alarms. Lower them, with care, to react faster.

## What happens on a failure

Each print is judged **once**. After a failure it stays flagged until the printer stops or starts a different file, so you're never spammed and a resumed print is never paused again.

1. **Reported everywhere:**
   - in **Activity** on the dashboard;
   - on the printer's **card** and in its **panel**;
   - in Discord's notification channel, with the camera picture;
   - on `/printer`.
2. **Paused** (only with `"action": "pause"`):
   - **Once:** the pause is sent a single time.
   - **Only while printing:** it's sent only if the printer still reports RUNNING at that moment. If the print already finished or was stopped, the report says so and nothing is sent.
   - **Confirmed before it says "paused":** it reports **paused** only once the printer itself reports `PAUSE`, waiting up to 30 seconds. If the printer doesn't confirm, the report says *"Pause sent, but the printer has not reported PAUSE yet"*, so you know to check.
   - **After a false alarm:** resume from the dashboard or with `/resume` if the print is fine.

With the default `"action": "notify"`, it only reports and never touches the printer.

## Setting up the AI HAT

You need a **Raspberry Pi 5** with the AI HAT+ (or AI Kit) fitted, on Raspberry Pi OS Bookworm (64-bit).

1. Update the Pi and install the Hailo software:

   ```bash
   sudo apt update && sudo apt full-upgrade -y
   sudo apt install -y hailo-all
   sudo reboot
   ```

2. Optionally, for faster inference, enable PCIe Gen 3. In `sudo raspi-config`, choose **Advanced Options → PCIe Speed → Yes**, then reboot.
3. Check that the HAT is found:

   ```bash
   hailortcli fw-control identify
   ```

   It should print the board name (for example `HAILO8L`) and firmware version.
4. Check that the service user can use it:

   ```bash
   sudo -u printermanager /usr/bin/python3 -c "import hailo_platform; print('ok')"
   ```

   If the device isn't readable, add the service user to the group that owns `/dev/hailo0` (shown by `ls -l /dev/hailo0`) and restart the service.

The AI part runs in a small helper (`failureDetection/hailo_worker.py`) under the **system** Python, `/usr/bin/python3`, because that's where `hailo-all` installs the Hailo library, numpy and Pillow. The service's own virtual environment doesn't need them.

## The model

Supply a `.hef` model **compiled for your HAT's chip**: Hailo-8L for the 13 TOPS AI Kit / AI HAT+, Hailo-8 for the 26 TOPS HAT+. Two kinds of model are understood:

- **Object detection with Hailo's on-chip NMS:** YOLO-style models from the Hailo Model Zoo / Dataflow Compiler. Each class gives boxes with scores.
- **Classification:** one score per class.

To train your own:

1. Train a small YOLO model (for example YOLOv8n) on pictures from your own printer cameras, labelled with classes such as `spaghetti`, `detached` and `blob`. Public "3D print failure" / "spaghetti" datasets are a good starting point.
2. Compile it with Hailo's Dataflow Compiler for your chip.

`classes` must list the model's classes **in output order**. `labels` names the classes that count as a failure. Leave it out to count every class.

Put the model somewhere the service can read, for example `/opt/3d-printer-management-models/print_failure.hef`. Don't use a folder under `/home`: the service is sandboxed with `ProtectHome=true` and can't see it.

## config.json

Add a `failure_detection` section (see [Configuration](../docs/configuration.md#config-json-reference)) and restart the service:

```json
"failure_detection": {
  "enabled": true,
  "model": "/opt/3d-printer-management-models/print_failure.hef",
  "classes": ["spaghetti", "detached", "blob"],
  "labels": ["spaghetti", "detached", "blob"],
  "threshold": 0.6,
  "interval": 30,
  "window": 10,
  "needed": 6,
  "min_minutes": 4,
  "warm_up": 3,
  "action": "notify"
}
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turn the feature on. |
| `model` | | Path to the `.hef` model. |
| `classes` | `["spaghetti"]` | The model's class names, in output order. |
| `labels` | all classes | Classes that count as a failure. |
| `threshold` | `0.6` | Score (0.05–0.99) at which a frame counts as failing. |
| `interval` | `30` | Seconds between frames per printer (at least 10). |
| `window` | `10` | How many recent frames are kept (3–60). |
| `needed` | `6` | Failing frames needed within the window. |
| `min_minutes` | `4` | Minimum minutes between the first and last failing frame. |
| `warm_up` | `3` | Minutes at the start of a print that aren't judged. |
| `action` | `"notify"` | `"notify"` only reports; `"pause"` also pauses the print. |
| `python` | `/usr/bin/python3` | The Python that has `hailo_platform` installed. |

Only printers with a camera (`camera_type` `rtsp` or `jpeg_tcp`) are watched.

## Dashboard and Discord

- **Printer card:** while watching a print, the card shows **🤖 AI watching**, **suspect frames**, **print may be failing** or **AI paused this print**, with the failing-frame count.
- **Printer panel → More → AI failure watch:** the current status, the last score and a tick box to **turn watching off for that printer**. This is useful for a print that confuses the model. It's saved on the Pi and recorded in Activity.
- **Discord `/printer`:** an *AI failure watch* line.
- **Notifications:** a red 🤖 message with the camera picture.

## Troubleshooting

| Shown | What to do |
|---|---|
| *AI HAT unavailable: Model file not found* | Check the `model` path and that the service user can read it. |
| *AI HAT helper did not start: No module named hailo_platform* | `sudo apt install hailo-all`, or point `python` at the Python that has it. |
| *…No Hailo device…* / *HAILO_OUT_OF_PHYSICAL_DEVICES* | Check `hailortcli fw-control identify`. Another program (for example `rpicam-apps` with Hailo) may be using the HAT; close it. |
| *No camera picture this time.* | The camera didn't answer; see [Camera](../docs/printer-controls.md#camera). |
| False alarms | Raise `threshold`, `needed` or `min_minutes`, or turn watching off for that printer. Better: add pictures of your own good prints to the training data. |

After a failed start, the helper is retried every 5 minutes. Errors are logged to the service log (`sudo journalctl -u 3d-printer-management`).
