# AI print-failure detection

The Pi can watch the printer cameras with an AI model and warn you, or pause the print, when a print looks like it has failed (spaghetti, a print knocked off the bed, a blob on the nozzle). It's **off** until you turn it on in `config.json`. The model runs in one of two places:

- **On the Pi's CPU:** a `.onnx` model, for example `best.onnx` straight from YOLOv8 training. No AI HAT and no compiling are needed. One small model every 30 s per printer takes well under a second on a Pi 5. See [Running on the CPU](#running-on-the-cpu-onnx).
- **On a Raspberry Pi AI HAT+ / AI Kit** (Hailo-8L or Hailo-8): a `.hef` model compiled for the chip. This frees the CPU, but compiling needs Hailo's tools on an x86 PC.

The app picks the place from the model file's extension.

- [Automatic setup](#automatic-setup)
- [Publishing a model for every Pi](#publishing-a-model-for-every-pi)
- [How it decides](#how-it-decides)
- [Better accuracy for your printers](#better-accuracy-for-your-printers)
- [Comparing with the print file](#comparing-with-the-print-file)
- [What happens on a failure](#what-happens-on-a-failure)
- [Automatic reprint on another printer](#automatic-reprint-on-another-printer)
- [Automatic model updates](#automatic-model-updates)
- [Running on the CPU (.onnx)](#running-on-the-cpu-onnx)
- [Setting up the AI HAT](#setting-up-the-ai-hat)
- [The model](#the-model)
- [config.json](#configjson)
- [Dashboard and Discord](#dashboard-and-discord)
- [Troubleshooting](#troubleshooting)

## Automatic setup

`install.sh` and `Updater/update.sh` set this up by themselves when a printer has a camera (`camera_type` `rtsp` or `jpeg_tcp`). They run `failureDetection/setup_ai.py`, which repeats everything that was first done by hand on the team Pi:

1. **CPU packages:** installs OpenCV, numpy and Pillow for the system Python (`python3-opencv`, `python3-numpy`, `python3-pil`).
2. **AI HAT, when one is on PCIe** (Hailo, vendor `0x1e60`):
   - installs `hailo-all` (or `hailo-h10-all` for a Hailo-10H), `dkms` and the kernel headers;
   - if `/dev/hailo0` is missing (the driver wasn't built for the running kernel, as happened on Raspberry Pi OS Trixie), reinstalls `hailort-pcie-driver` so it's rebuilt;
   - if the driver only loads after a reboot, says **REBOOT RECOMMENDED** and uses the CPU until then.
3. **Model:** downloads it from the repository's [`ai-model` release](#publishing-a-model-for-every-pi) into `/opt/3d-printer-management-models/`, checked against GitHub's SHA-256.
   - It picks `print_failure_<chip>.hef` for a working AI HAT, otherwise `print_failure.onnx`.
   - **A model you copied there yourself is never replaced.** To go back to the release model, delete yours and run the setup again.
4. **Test:** pushes one test picture through the real helper as the service user. A model that doesn't run is never enabled.
5. **Config:** adds a `failure_detection` section to `config.json`, **only if there isn't one**, with `"action": "notify"`. Your own settings are never changed. The service is then restarted.

It never stops an install: each step reports and carries on.

**Running it by hand,** for example after adding a camera, fitting an AI HAT or rebooting:

```bash
sudo /usr/bin/python3 /opt/3d-printer-management/failureDetection/setup_ai.py --restart
```

**Skipping it:** set `PM_AI=0`. **Using another repository's models:** set `PM_AI_REPO=owner/name`. By default it follows **Settings → GitHub releases**, including a private token.

Updates installed from the dashboard don't run it, because they never run scripts as root. The next `update.sh` or install does.

## Publishing a model for every Pi

Every Pi fetches its model from a GitHub release tagged **`ai-model`** in the repository it updates from. The dashboard's updater ignores this tag, because it isn't a version number. Attach:

| File | |
|---|---|
| `print_failure.onnx` | YOLOv8 export (`yolo export model=best.pt format=onnx opset=11`). Runs on the CPU. |
| `print_failure.json` | Optional: `{"classes": [...], "labels": [...], "threshold": 0.4, "input_size": 640}`. Without it, the classes default to `spaghetti, stringing, warping` with `spaghetti, warping` counted and threshold 0.4. |
| `print_failure_hailo8.hef` / `_hailo8l` / `_hailo10h` | Optional: the same model compiled for an AI HAT chip. |

To publish a better model, replace the asset. Pis that downloaded the old one get the new one on their next update; Pis with a model copied in by hand keep theirs. If the dataset's licence asks for credit (CC BY 4.0 does), give it in the release notes.

## How it decides

One bad-looking picture is never enough. While a printer reports **RUNNING**, the Pi takes a camera still every few seconds. The interval adapts to how busy the Pi is: 5–60 s with the AI HAT, 10–60 s on the CPU. The model scores each still.

A frame **looks failed** when its score reaches the **failure score** (`threshold`).

The AI's score for a real failure flickers: the same spaghetti nest scores 45%, then 30%, then 42% from one frame to the next. So once **two** frames have reached the failure score, later frames at or above the lower **keep counting** score (`hold`, by default 0.15 below the threshold) count as failing too.

A print is called a failure when **all** of these hold:

| Rule | Default |
|---|---|
| At least this share of the frames from the last 5 minutes look failed (`window` × `needed`) | 60% |
| The failing frames span at least `min_minutes` | 4 minutes |
| At least two frames reached the failure score | |
| The newest frame is still at or above the keep-counting score | |
| The print has been running for at least `warm_up` minutes, because heat-up, purge lines and the first layer can look odd | 3 minutes |

How odd frames are handled:

- **Duplicate frames:** a frame identical to the previous one (a frozen camera, or a reused still) is skipped and isn't counted twice.
- **Camera gaps:** if no frame arrives for a few minutes, the evidence starts again.
- **Suspect frames:** two or more failing frames that don't yet meet the rules show **suspect** on the dashboard. Nothing is sent.

### Sensitivity (per printer, in the dashboard)

Open a printer → **More → AI failure watch → Sensitivity**. Pick a preset, or type your own numbers (that switches the preset to **Custom**), then press **Save sensitivity**. The text under the fields explains what the numbers mean. Settings are kept per printer in `settings.json` (`ai_tuning`).

| Preset | Failure score | Keep counting from | Must last | Share of frames |
|---|---|---|---|---|
| Cautious | 60% | 50% | 6 min | 70% |
| Normal | config.json (`threshold`, `hold`, `min_minutes`, `window`/`needed`) | | | |
| Sensitive | 35% | 20% | 3 min | 40% |
| Very sensitive | 25% | 12% | 2 min | 30% |

When failures score only 30–45% on your camera, as on the A1 mini, try **Sensitive**. If it raises false alarms, raise the failure score a little or make it last longer. With a `.hef` model, the AI HAT reports nothing under 25% (its on-chip cut-off), so a lower setting acts like 25%.

### Zooming in on the print (automatic)

The failure model only knows what failures look like, not where the bed is. So the monitor uses the model's own detections to find the print:

- **Suspicious spots:** when a failure detection of 10% or more turns up, the next checks add a close-up around it, 2.5× its size, for 10 minutes. A small nest that scores 30% in the whole picture often scores much higher enlarged, which gets it over the failure score.
- **The print's area:** a model trained with a `print` or `bed` class (also `object`, `part`, `plate`) gets a close-up around that area. These classes never count as failures. To get one, label the printed part as `print` in Roboflow along with the failures, and retrain.
- **Status:** the AI panel shows 🔍 when it's zooming in.

You can turn this off per printer in **Sensitivity**. `"focus": false` under `failure_detection` turns it off by default.

## Better accuracy for your printers

A model trained on other people's photos can miss failures that are obvious to you on your own printers. That happened on an A1 mini: its camera looks across the bed from low down, the plate was a shiny holographic one, and the toolhead light caused glare. A clear spaghetti mess at the back of the bed scored only 0.13, while the model reacted weakly to the glittery plate instead. Two things fix that.

### 1. Close-ups (automatic)

The model shrinks every picture to 640×640 by its longer side, so a mess at the back of the bed ends up only a few dozen pixels tall. So each check also looks at enlarged close-ups and keeps the strongest result:
- **Calibrated camera:** the bed outline, widened upwards for tall parts and split in two when the bed is wide.
- **Uncalibrated camera:** the upper part of the frame, where the bed and print are on side-mounted cameras like the A1 and A1 mini.

That makes the print area about 1.6–2× bigger to the model, without the table and base. Each check takes a few hundred milliseconds longer on the CPU. Turn it off with `"crops": "off"` under `failure_detection`.

### 2. Train on your own pictures (the real fix)

While a printer prints, the app keeps training pictures from its camera:
- a still **every minute**;
- **every frame the AI found suspicious** (every check, 30 s apart);
- capped at 5,000 pictures, about 1 GB (oldest removed first), and never saved when less than 2 GB is free.

To use them:
1. **Download:** open any printer → **More → AI failure watch → Download training pictures**. The ZIP is sorted into `failed/`, `finished/` and `other/` by how each print ended.
2. **Upload:** in Roboflow, open your project → **Upload**, and drag the folders in.
3. **Label:** draw a box around every spaghetti, warping or stringing area, labelled with that name. **Leave good prints without boxes**: they teach the model what normal looks like on your plates and in your lighting.
4. **Retrain:** generate a new dataset version and run the Colab training cell again (see [Running on the CPU](#running-on-the-cpu-onnx)).
5. **Install the new model:** copy the new `best.onnx` over `/opt/3d-printer-management-models/print_failure.onnx`, or publish it in the [`ai-model` release](#publishing-a-model-for-every-pi) so every Pi updates itself.

Even 50–100 labelled pictures from your own cameras usually make a big difference. Spaghetti tests are the quickest way to get failure pictures. Settings (optional): `"collect": {"enabled": true, "every_minutes": 1, "max_pictures": 5000}`. With several printers going, 5,000 pictures is roughly a day or two of printing; download them before they roll over.

## Comparing with the print file

The model only sees a picture. It can't know whether a stringy-looking shape is spaghetti or the part's own supports and thin walls. The print file does know: the sliced G-code says exactly where plastic goes, layer by layer. With a **calibrated camera**, the app compares each detection with it:

| Where the detection is | What the app does |
|---|---|
| **Off the part**: the file puts no plastic there (with a margin) | Keeps the full score. Material where nothing should be printed is strong evidence of spaghetti or a part knocked loose. The report says *"outside where the print file puts plastic"*. |
| **On the part**: plastic is expected there by the current layer | Counts it at half its score (`on_part_weight`). It's more likely the part's own geometry, which cuts false alarms on supports, lattices and thin features. A real failure on the part still gets through when the model is confident. |

**How it works:**
- **Reading the file:** when a print starts from the **queue** or **Print now**, the app reads that plate's G-code once, in a low-priority background process. It builds a 2 mm grid of where each layer puts plastic and how tall the part is; large files take a minute or so on a Pi. The grid is cached in `/var/lib/3d-printer-management/geometry/`.
- **Matching:** each check uses the printer's reported layer number, so only plastic printed *so far* counts.
- **Calibration** (once per camera): open the printer → **More → AI failure watch → Calibrate camera to bed…** and click the printable area's four corners on the picture: **front-left** (where X and Y are 0), **front-right**, **back-right**, **back-left**.
  - **Accuracy:** exact on the bed surface. For taller parts, the area around the part is widened by the part's height, because the camera sees the top of a tall part further from its base.
  - **Recalibrate** after moving or bumping the camera.
- **When it's skipped:** prints started from Bambu Studio or the printer's screen (the app doesn't have their file), an uncalibrated camera, or a model without boxes (classification). In those cases scores are used as they are, exactly as before.

Settings (optional), under `failure_detection`:

```json
"geometry": {"enabled": true, "on_part_weight": 0.5, "margin_mm": 5}
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Compare with the print file when possible. |
| `on_part_weight` | `0.5` | Share (0–1) of the score a detection on the part keeps. `1` treats on-part and off-part alike. |
| `margin_mm` | `5` | How close to printed plastic still counts as "on the part". The part's height is added to it. |

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

## Running on the CPU (.onnx)

1. **Install OpenCV** for the system Python. It's a Raspberry Pi OS package; the app's own Python environment doesn't change.

   ```bash
   sudo apt install -y python3-opencv
   ```

2. **Get a YOLOv8 `.onnx` model**, trained at the default 640 px:

   ```bash
   yolo export model=best.pt format=onnx opset=11
   ```

   The [3D Print Failure Detection](https://universe.roboflow.com/purvi-rathore-5amqh/3d-print-failure-detection-efvsh/dataset/4) dataset (CC BY 4.0) trains well in about 15–25 minutes on Google Colab's free GPU. Training on the Pi itself takes over a day.

3. **Copy it** somewhere the service can read:

   ```bash
   sudo mkdir -p /opt/3d-printer-management-models
   sudo cp best.onnx /opt/3d-printer-management-models/print_failure.onnx
   sudo chmod 644 /opt/3d-printer-management-models/print_failure.onnx
   ```

4. **Configure it** in config.json, then `sudo systemctl restart 3d-printer-management`:

   ```json
   "failure_detection": {"enabled": true, "model": "/opt/3d-printer-management-models/print_failure.onnx",
                         "classes": ["spaghetti", "stringing", "warping"], "labels": ["spaghetti", "warping"],
                         "action": "notify"}
   ```

   - **`classes`:** must match the `names:` list in the dataset's `data.yaml`, in the same order.
   - **`stringing`** is left out of `labels`: it's cosmetic, and counting it would cause false alarms.

The helper limits OpenCV to 2 threads, so the dashboard, MQTT and cameras stay responsive. A model trained at a size other than 640 px needs `"input_size"` set to match.

## Automatic reprint on another printer

When the AI **pauses** a failing queue print, the job can carry on somewhere else:

1. **The pause notification** says which printers could take it, for example *"Available for a reprint: Mini 2 (bed checked empty)"*, or why none can.
2. **Countdown:** if nobody resumes or stops the paused print within **12 hours**, the app reprints the job on an available printer:
   - it stops the paused print, so the printer doesn't sit paused and heated;
   - records the job as failed;
   - starts a copy on the chosen printer;
   - says so in Discord and Activity.
3. **Reprint now:** open the printer → **More → AI failure watch → Reprint now…** does the same at once. **Cancel automatic reprint** stops the countdown, and **Check available printers** looks again.
4. **No printer free when it's time:** it keeps waiting, tries again every minute, and tells you once why.

**What makes a printer available:**
- it's the **model the file was sliced for**;
- it's online, idle or finished, with **no error** and **no active queue job**;
- its loaded AMS filament matches the plate (or the job used the external spool);
- its **bed is empty**, judged by the camera.

**How the bed check works:** every time someone starts a print and confirms *"the plate is clear"*, the app takes a fresh picture of that empty bed. Later it compares a new picture with it, inside the calibrated bed outline when there is one, with lighting differences evened out.
- It errs towards "parts on the bed": a printer with no empty-bed picture yet, or an unclear result, is never used automatically.
- Calibrating the camera ([above](#comparing-with-the-print-file)) makes the check more precise.

The countdown is saved in `/var/lib/3d-printer-management/ai-reprints.json`, so it survives restarts. It's only for prints the AI paused (`"action": "pause"`) and for queue jobs; a print started from Bambu Studio has no file on the Pi to reprint. Settings (optional), under `failure_detection`:

```json
"auto_reprint": {"enabled": true, "after_hours": 12, "stop_original": true, "bed_threshold": 0.005}
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Offer and run automatic reprints. |
| `after_hours` | `12` | How long a paused print waits before it's reprinted elsewhere. |
| `stop_original` | `true` | Stop the paused print when reprinting. |
| `bed_threshold` | `0.005` | Share of the bed that may differ from the empty-bed picture and still count as empty. |

## Automatic model updates

Once a day the dashboard service checks the repository's [`ai-model` release](#publishing-a-model-for-every-pi). When it has a newer `print_failure.onnx`, the service:

1. downloads it into `/var/lib/3d-printer-management/models/`;
2. checks it runs with one test picture;
3. switches to it straight away, with no restart and no commands.

This works for updates installed from the dashboard too. It only replaces a model that came from that release; one you copied in yourself or pointed config.json at is never touched. The printer panel shows the model in use.

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

## Installing a model for the AI HAT

### Getting a .hef without a Hailo account

The [Ultralytics Platform](https://platform.ultralytics.com) can compile your trained model for the AI HAT:

1. Upload your `best.pt`.
2. Choose **Export → Hailo** with target **Hailo-8** (26 TOPS HAT+) or **Hailo-8L** (13 TOPS AI Kit / HAT+).
3. Download `best.zip`. It contains:
   - `best_hailo_model/best.hef`;
   - `metadata.yaml`, which holds the class names in training order;
   - `nms_config.json`, which holds the on-chip thresholds.

Two things to know about this export:

- **On-chip cut-off.** The HAT drops every detection below `nms_scores_th` (0.25). A picture the CPU model scores at 0.13 scores **0** on the HAT. This doesn't matter for a `threshold` of 0.25 or higher.
- **Generic calibration.** The 8-bit quantisation was calibrated on COCO128, which contains no printer pictures. Compare it with the `.onnx` on your own failure pictures before you trust it (`--failure` below). Exporting again with your own pictures as calibration data improves this.

### Installing it

Copy `best.zip` to the Pi, then run:

```bash
sudo /usr/bin/python3 /opt/3d-printer-management/failureDetection/install_model.py best.zip \
    --failure ~/pictures/failed --healthy ~/pictures/good
```

Both picture folders are optional. They can be anywhere: the installer reads them and sends them to the model.

The installer does the following. If any check fails, it says why and **changes nothing**.

1. **Reads the export.** It takes the class names from `metadata.yaml` in training order, and the NMS thresholds from `nms_config.json`. A plain `.hef` or `.onnx` also works; give `--classes spaghetti,stringing,warping` when there's no metadata.
2. **Checks the model against this Pi.** It runs `hailortcli parse-hef` and `hailortcli fw-control identify`. The model must:
   - be compiled for this HAT's chip;
   - take one UINT8 NHWC square input;
   - end in Hailo NMS;
   - have as many classes as names.

   It also notes the HailoRT and firmware versions and the CPU architecture.
3. **Test-runs it** through the real helper as `printermanager`, with a time limit. It uses a blank picture plus your folders, and reports how many failure pictures it catches and how many good ones it flags. `--min-catch 0.8` or `--max-false 0.1` refuse a model that does worse.
4. **Installs it with backups.**
   - The old model goes to `/opt/3d-printer-management-models/backups/`.
   - `config.json` is copied to `config.json.backup-<time>`.
   - The new file is installed as `print_failure.hef`, root-owned, mode 644.
   - A record is written next to it in `print_failure.hef.json`: SHA-256, classes, thresholds, chip, calibration data, HailoRT version and test results.
   - In `config.json`, only `failure_detection.model` changes, plus `classes` and `input_size` when the model's differ. The file keeps its owner and permissions.
   - The `.onnx` stays in place as the fallback.
5. **Restarts the service and verifies it.** It waits for the app's own log line `AI model ready: print_failure.hef on AI HAT (Hailo-8)`. If the model doesn't load, or the app falls back to the CPU, it **rolls back** automatically and restarts again.

Other options:

- `--check-only` runs steps 1 to 3 and changes nothing.
- `--rollback` undoes the last install.

### Which processor is in use

- **Log.** At start-up the app logs `AI model ready: <model> on AI HAT (Hailo-8)` or `… on CPU`.
- **AI panel.** It shows **Running on: …**.
- **Fallback.** If a `.hef` won't start, for example because the HAT driver didn't load after a kernel update, the app logs `AI HAT model … did not start …; falling back to the CPU model print_failure.onnx`. It then keeps watching on the CPU at the CPU's pace, and the AI panel shows a ⚠️ note.
  - The fallback is the `.onnx` with the same name next to the `.hef`, or `failure_detection.fallback_model`.

## The model

For the CPU, use a YOLOv8 `.onnx` export (see [above](#running-on-the-cpu-onnx)). For the AI HAT, supply a `.hef` model **compiled for your HAT's chip**: Hailo-8L for the 13 TOPS AI Kit / AI HAT+, Hailo-8 for the 26 TOPS HAT+. Two kinds of model are understood:

- **Object detection with Hailo's on-chip NMS:** YOLO-style models from the Hailo Model Zoo / Dataflow Compiler. Each class gives boxes with scores.
- **Classification:** one score per class.

To train your own:

1. Train a small YOLO model (for example YOLOv8n) on pictures from your own printer cameras, labelled with classes such as `spaghetti`, `detached` and `blob`. Public "3D print failure" / "spaghetti" datasets are a good starting point.
2. Export it to `.onnx` and use it on the CPU straight away, or compile it with Hailo's Dataflow Compiler for your HAT's chip.

`classes` must list the model's classes **in output order**. `labels` names the classes that count as a failure. Leave it out to count every class.

Put the model somewhere the service can read, for example `/opt/3d-printer-management-models/print_failure.onnx` or `.hef`. Don't use a folder under `/home`: the service is sandboxed with `ProtectHome=true` and can't see it.

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
| `model` | | Path to the model: `.onnx` runs on the CPU, `.hef` on the AI HAT. |
| `classes` | `["spaghetti"]` | The model's class names, in output order. |
| `labels` | all classes | Classes that count as a failure. |
| `threshold` | `0.6` | Score (0.05–0.99) at which a frame counts as failing. |
| `interval` | `30` | Seconds between frames per printer (at least 10). |
| `window` | `10` | How many recent frames are kept (3–60). |
| `needed` | `6` | Failing frames needed within the window. |
| `min_minutes` | `4` | Minimum minutes between the first and last failing frame. |
| `hold` | `threshold` − 0.15 | Once two frames reached `threshold`, frames at or above this keep counting as failing. |
| `focus` | `true` | Add close-ups around suspicious spots (and a `print`/`bed` class) automatically. |
| `warm_up` | `3` | Minutes at the start of a print that aren't judged. |
| `action` | `"notify"` | `"notify"` only reports; `"pause"` also pauses the print. |
| `python` | `/usr/bin/python3` | The Python that has OpenCV (`.onnx`) or `hailo_platform` (`.hef`) installed. |
| `input_size` | `640` | `.onnx` only: the image size the model was trained at. |

Only printers with a camera (`camera_type` `rtsp` or `jpeg_tcp`) are watched.

## Dashboard and Discord

- **Printer card:** while watching a print, the card shows **🤖 AI watching**, **suspect frames**, **print may be failing** or **AI paused this print**, with the failing-frame count.
- **Printer panel → More → AI failure watch:** the current status, the last score and a tick box to **turn watching off for that printer**. This is useful for a print that confuses the model. It's saved on the Pi and recorded in Activity.
- **Discord `/printer`:** an *AI failure watch* line.
- **Notifications:** a red 🤖 message with the camera picture.

## Troubleshooting

| Shown | What to do |
|---|---|
| *AI unavailable: Model file not found* | Check the `model` path and that the service user can read it. |
| *AI helper did not start: No module named 'cv2'* | `.onnx` model: `sudo apt install python3-opencv`. |
| *AI helper did not start: No module named hailo_platform* | `.hef` model: `sudo apt install hailo-all`, or point `python` at the Python that has it. |
| *…No Hailo device…* / *HAILO_OUT_OF_PHYSICAL_DEVICES* | Check `hailortcli fw-control identify`. Another program (for example `rpicam-apps` with Hailo) may be using the HAT; close it. |
| *No camera picture this time.* | The camera didn't answer; see [Camera](../docs/printer-controls.md#camera). |
| *Running on: CPU ⚠️ AI HAT model failed to start* | The `.hef` didn't load and the `.onnx` is used. Check `hailortcli fw-control identify`; after fixing it, restart the service. |
| Failures missed, scores stay around 30–45% | Choose **Sensitive** under Sensitivity, and keep **Zoom in on the print** on. Best: train on your own pictures. |
| False alarms | Choose **Cautious**, or raise the failure score or how long it must last, or turn watching off for that printer. Better: add pictures of your own good prints to the training data. |

After a failed start, the helper is retried every 5 minutes. Errors are logged to the service log (`sudo journalctl -u 3d-printer-management`).
