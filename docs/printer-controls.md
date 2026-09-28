# Printer controls & camera

You can change temperatures, speed, fans and lights, and jog the axes, from the dashboard (click a printer card to open its panel) or from Discord. In Discord, `/speed`, `/fan`, `/fanall` and the light commands are open to everyone (with confirmation and logging), while `/temperature`, `/chamber` and `/move` are admin-only by default. See [Commands and permissions](commands.md).

**Confirmations:** in the dashboard, only **Pause** and **Stop** ask first. Everything else applies straight away, and the limits below are still enforced by the server. In Discord, every change still shows a confirmation card. A change is **sent** over MQTT, which doesn't mean it was **applied**: the firmware can still reject it. If the printer rejects a command, it's shown in Activity and posted to the notification channel. Otherwise, check the printer's telemetry or screen to confirm.

In demo mode, controls only change the simulated data.

- [Pause, resume, stop and light](#pause-resume-stop-and-light)
- [Temperatures](#temperatures)
- [Chamber heating (H2D)](#chamber-heating-h2d)
- [Print speed](#print-speed)
- [Fans](#fans)
- [Moving the axes](#moving-the-axes)
- [Camera](#camera)

## Pause, resume, stop and light

| Action | Dashboard | Discord |
|---|---|---|
| Pause | **Pause**, then confirm | `/pause` |
| Resume | **Resume** (no confirmation) | `/resume` |
| Stop (cancel the print) | **Stop**, then confirm | `/stop`, then confirm |
| Chamber light | 💡 button (pulses purple until the printer reports the change, then amber when on) | `/lighton`, `/lightoff` |

**Stop and the queue:**
- **Before the file was sent** (the job is still staging): stopping cancels the job.
- **After the print command may have been sent:** the job becomes *needs review*. See [Print queue](print-queue.md#when-a-job-needs-review).

Light control uses the standard `chamber_light` command. Whether it works depends on the model and firmware.

## Temperatures

| | Dashboard | Discord |
|---|---|---|
| Nozzle | Control: *Active nozzle temperature* | `/temperature target:nozzle degrees:210` |
| Bed | Control: *Bed temperature* | `/temperature target:bed degrees:60` |

- **Values:** whole degrees Celsius, and **0 turns heating off**.
- **Limits:** set per model. See [Configuration](configuration.md#printer-entries).

  | Model | Nozzle max | Bed max |
  |---|---|---|
  | H2D | 350 °C | 120 °C |
  | A1 | 300 °C | 100 °C |
  | A1 mini | 300 °C | 80 °C |
  | unknown | 300 °C | 80 °C |

  Set `model` on each printer so the right limits apply.
- **Nozzle:** the *active* nozzle is changed. The app doesn't switch H2D tools.
- **How it's sent:** as non-blocking `M104`/`M140` commands. The G-code of a running print can change the targets again.

## Chamber heating (H2D)

`/chamber degrees:60`, `/temperature target:chamber degrees:60`, or the dashboard control *Heated chamber*.

- **Range:** 0 turns chamber heating off; otherwise 40–65 °C.
- **H2D only.** Other models refuse it.
- **Rejections:** the firmware refuses chamber heating while low-temperature filament (PLA/PETG/TPU) is loaded, and the app shows that reason.

## Print speed

`/speed mode:sport`, or the dashboard control *Print speed*. Profiles: **Silent**, **Standard**, **Sport**, **Ludicrous**.

## Fans

- **One fan:** `/fan percent:50 target:part`, or drag that fan's slider in the dashboard (it's set when you let go). Autocomplete and the dashboard list the fans **this printer reports**.
- **All fans on one printer:** `/fanall percent:80` or *All manual fans*. The result lists any automatic fans it skipped.

How fans are controlled:
- **Newer firmware:** uses the reported air-duct fan IDs, ranges and manual-control flags with `set_fan`.
- **Older reports:** use the part, auxiliary and chamber `M106` channels.
- **Firmware-managed fans:** the hotend heatbreak and electronics fans are never changed. Fans in automatic mode aren't forced either. Change the airflow mode on the printer if you need manual control.

## Moving the axes

**Homing:** **⌂ Home** in the dashboard's printer panel, or Discord `/home` (admins, with confirmation), homes X, Y and Z.
- **When it's allowed:** only while the printer is idle, connected and error-free, with no active queue job.
- **Command used:** printers that report MQTT homing support (bit 32 of the `fun` flags) get Bambu Studio's `back_to_center` command. Others get `G28`.
- **Hardware check needed:** this command choice follows Bambu Studio and still needs checking on each model.
- **Before moving:** let homing finish before you move any axis.

Discord `/move axis:X millimeters:1`, or the **Move axes** pad in the dashboard's printer panel.

**In the dashboard:** the pad is locked until you tick *The printer is homed, the travel path is clear and I am watching it*. It stays unlocked until the panel closes. After each move the buttons count down the 3-second gap before the next one.

**Before moving:**
- **Home the printer** from its own screen.
- **Check the nozzle, bed and travel path are clear.**
- **Watch the printer while it moves.** The app can't see whether the printer is homed or whether anything is in the way.

The app refuses to move unless all of these hold:
- the printer is IDLE or FINISH, connected, and without errors
- the last telemetry is under 15 seconds old
- no queue job is active or waiting for review
- at least 3 seconds have passed since the previous move

**Distances depend on the printer:**

| Printer | How it's moved | Allowed distances |
|---|---|---|
| Newer firmware that reports MQTT axis control (e.g. H2D) | Bambu's `xyz_ctrl` command, the same as Bambu Studio | X/Y: **±1 or ±10 mm**; Z: **±1 mm** |
| Other printers (e.g. A1 / A1 mini) | Relative G-code, wrapped like Bambu Studio (soft endstops on, movement mode restored afterwards) | X/Y: ±0.1–10 mm; Z: ±0.1–1 mm |

- **Directions** follow printer coordinates: +Z doesn't necessarily mean the bed moves up.
- **What it never does:** extrude, home automatically, change motor current, or bypass endstops.
- **Not yet tested on physical hardware:** the movement fix. Try a 1 mm move first on each model.

## Camera

Set `camera_type` on each printer in [config.json](configuration.md#printer-entries):

| `camera_type` | Printers | How it works |
|---|---|---|
| `jpeg_tcp` | A1 family | TLS JPEG stream on port 6000. Frame rate is limited by the camera. |
| `rtsp` | H2D and other RTSP cameras | RTSP on port 322, decoded by **ffmpeg** (limited to 5 fps at 960 px wide to spare the Pi) |

- **Snapshots:** `/printer`, notifications, and **Snapshot → Refresh camera snapshot** in the dashboard.
- **Live view:** starts automatically when you open a printer's panel, and shows about one frame per second. The Overview cards also show a camera image that refreshes every few seconds. It isn't full-motion video, and nothing is recorded.
  - **One shared connection:** all viewers and snapshots share one camera connection per printer. It reconnects automatically after errors, and closes when nobody is watching.
  - **When it stops:** closing the panel, hiding the browser tab or signing out stops your live view. Card images only refresh while the Overview tab is on screen.
- **Login required:** camera access needs a dashboard login, and the session is re-checked while streaming.
- **Other clients:** if the printer won't accept another camera connection, close other camera clients (Bambu Studio, Handy).
- **Printer settings:** firmware LAN settings and the access code still decide whether the camera is available. Camera errors are logged with the access code removed.
