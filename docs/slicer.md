# Slicer

The **Slicer** tab turns STL files into a sliced plate that a printer can print, without Bambu Studio or OrcaSlicer on your computer. You build the plate in the browser, and **OrcaSlicer runs on one or more worker computers** (for example spare laptops running Ubuntu Server). The Pi keeps the queue, hands each job to a free worker, and keeps the sliced file. You can download it or send it straight to a printer's print queue.

The Pi itself never slices: OrcaSlicer is too heavy for it, and its official Linux build is x86_64 only.

- [How it works](#how-it-works)
- [Setting up a worker](#setting-up-a-worker)
- [Slicing a plate](#slicing-a-plate)
- [Workers](#workers)
- [What it can't do yet](#what-it-cant-do-yet)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [Licences](#licences)

## How it works

```
 Browser (Slicer tab)  ──upload STL, plate, presets──▶  Pi (this app)  ◀──ask for work, send results──  Worker computers
   three.js 3D view       ◀──live job/worker status──    queue (SQLite)                                   OrcaSlicer 2.4.2
```

1. **Upload:** each STL is checked on the Pi (a real STL, at most 200 MB and 3 million triangles, no invalid numbers) and kept once, by its content.
2. **Plate:** you place, rotate and scale the objects in the 3D view. Moving things never slices anything.
3. **Slice:** the Pi checks the request (known models, presets that belong together, settings within their limits) and queues it.
4. **Worker:** a free worker takes the oldest job and downloads its models. It then places each model exactly where you put it, writes the presets with your changes, and runs OrcaSlicer's command line. Every job runs in its own temporary folder, which is deleted afterwards.
5. **Result:** the worker checks that OrcaSlicer wrote a sliced 3MF with G-code and sends it back with the print time, filament and weight. The Pi checks it again before keeping it.

**When a worker disappears.** Each job a worker takes comes with a **lease** that lasts 90 seconds. The worker renews it every 15 seconds while it works. If a worker crashes, loses its network or is switched off, its job goes back in the queue and another worker picks it up. A job is tried 3 times at most. A late answer from the old worker is refused, so a job is never completed twice.

**Errors in the model or settings**, such as objects off the plate or a setting OrcaSlicer rejects, fail the job straight away with OrcaSlicer's message. **Trouble on the worker's side**, such as OrcaSlicer crashing or taking longer than 30 minutes, puts the job back in the queue for another try.

## Setting up a worker

**Requirements:** an x86_64 computer running **Ubuntu 24.04 or newer, or Debian 13 or newer** (OrcaSlicer's Linux build needs their newer system libraries; Ubuntu 22.04 works only if the OrcaSlicer release has a 22.04 build, and the installer's test slice tells you). A desktop isn't needed: Ubuntu Server is fine. It needs 4 GB of RAM or more (8 GB for big plates) and a few GB of disk, on the same network as the Pi.

1. In the dashboard, open **Slicer → Workers → Add a worker**. You get a command with a one-time setup code that is valid for 24 hours.
2. Run that command on the worker computer. It looks like this:

   ```bash
   curl -fsSL http://PI-ADDRESS:8080/slicer/worker/install.sh | sudo bash -s -- --server http://PI-ADDRESS:8080 --code SETUP-CODE
   ```

The installer:

- installs the packages OrcaSlicer needs, including `xvfb`, because OrcaSlicer's command line still needs a virtual display;
- downloads **OrcaSlicer 2.4.2** from its official GitHub release (the Linux AppImage) and unpacks it in `/opt/orcaslicer`;
- downloads the worker from the Pi into `/opt/orca-worker` and registers it with the setup code;
- **slices a 20 mm test cube** to prove OrcaSlicer works on that computer;
- installs the `orca-worker` service. It runs as its own `orcaworker` user, is limited to 6 GB of memory (`--memory-max 8G` changes that), can only write to `/var/lib/orca-worker`, and is restarted if it stops.

The worker then shows under **Slicer → Workers**, and its presets appear in the Slicer tab within a few seconds.

**Updating:** run the same command again without `--code`. It updates the worker from the Pi and, when this app moves to a newer OrcaSlicer version, OrcaSlicer too.

Other options: `--name "Laptop 3"` (the name shown in the dashboard), `--orca-url URL` (a different download of the Linux AppImage, if GitHub can't be reached).

**Removing a worker:** **Remove** in the dashboard, then on the computer run `sudo systemctl disable --now orca-worker`.

## Slicing a plate

1. **Add STL files** with the button, or drop them on the plate.
2. **Arrange them:**
   - Click an object to select it. Drag the arrows to move it, or use the **Move / Rotate / Scale** buttons (keys **G**, **R**, **S**).
   - Type exact numbers under **Selected**: position in mm, rotation in degrees, scale in %. **Keep proportions** scales all three axes together.
   - **Duplicate** (Ctrl+D), **Delete**, **Reset** (back to where it was added), **Drop to bed**, and **Arrange**, which lays all the objects out in rows.
   - Objects off the plate, taller than the printer, or overlapping another object turn red and are listed under the plate.
3. **Choose presets.** The printer, process and filament lists are OrcaSlicer's own Bambu Lab presets, and only matching ones are offered. The printer starts on the model of your first printer.
4. **Change settings** if you need to (blank means "as in the preset"):

   | Group | Settings |
   |---|---|
   | **Quality** | layer height, first layer height, line width, seam position, ironing |
   | **Strength** | wall loops, top and bottom shell layers, infill density and pattern |
   | **Speed** | outer and inner wall, infill, first layer and travel speeds, acceleration |
   | **Support** | supports on or off, support type, overhang angle, build plate only, brim |
   | **Material** | nozzle temperatures, plate temperatures, flow ratio, max volumetric speed, max fan speed |

   Every value is checked on the Pi against its limits, so a worker only ever gets known settings.
5. **Slice plate.** The job appears under **Sliced plates** with its place in line and its progress: *Waiting for a worker*, *Preparing*, *Slicing* or *Checking the result*. You can leave the page while it slices.
6. **When it's sliced**, the job shows the print time, filament and weight, and any warnings from OrcaSlicer.
   - **Download** saves the `.gcode.3mf`.
   - **Send to print queue** puts it in the chosen printer's queue, just like an upload. It still waits there until you press **Start** with a clear plate, and the usual check that the file was sliced for that printer's model applies.

## Workers

Each worker shows whether it is online, its CPU, free memory, load, free disk, operating system, and its OrcaSlicer and worker versions. For each worker you can set:

- **On:** turned off, it takes no jobs.
- **Drain:** it finishes what it's slicing but takes nothing new. Use it before turning a computer off.
- **Jobs at once:** 1 by default. Raise it only on computers with plenty of memory and cores.
- **Remove:** it has to be set up again with a new code to come back.

## What it can't do yet

This is the first part of the slicer. Not there yet:

- **STL only:** no 3MF, OBJ or STEP import.
- **No toolpath preview:** the sliced plate can be checked in Bambu Studio or OrcaSlicer if needed.
- **No supports painting, seams or modifiers, and no cutting.**
- **One plate per job.** You arrange the objects yourself (or use **Arrange**); OrcaSlicer's own arrange isn't used, so objects stay where you put them.
- **Single filament:** one filament preset per plate, so no multi-colour AMS plates.
- **Bambu Lab printer presets only**, and no saved presets of your own. Changed settings are kept per job.
- **One dashboard login**, like the rest of the dashboard, so there are no per-user jobs or roles.

## Troubleshooting

| Problem | What to do |
|---|---|
| *No slicing worker has reported its presets yet* | Set up a worker. If one is set up, check that it's online under **Workers** (`sudo systemctl status orca-worker` on it). |
| Worker shows 🔴 offline | On the worker: `sudo journalctl -u orca-worker -n 50`. Check it can reach the Pi's address and port. |
| The installer's test slice fails | Its output shows OrcaSlicer's error. Usually a missing library: run the installer again; on unusual distributions install `libwebkit2gtk-4.1-0` and `libgtk-3-0` by hand. |
| A job fails with *OrcaSlicer: …* | That's OrcaSlicer's own message, for example objects outside the plate or too close together. Fix the plate or the settings and slice again. |
| A job keeps going back in the queue | The worker is crashing or running out of memory: see its log, lower **Jobs at once**, or give it more memory with `--memory-max`. |
| *Less than 1 GB of free disk space on the Pi* | Uploaded models and sliced plates are in the app's data folder under `slicer/`. Free some space. |

## Security

- **Workers have their own tokens.** A worker gets a random token when it registers, and the Pi stores only its hash. Setup codes work once and expire after 24 hours. The workers' part of the API only accepts a valid worker token, and only for the jobs that worker holds (its lease).
- **Nothing from a job is run.** Workers get plain numbers (transforms), preset names and checked settings. OrcaSlicer is started with an argument list (no shell) on files the worker wrote itself. The service runs as an unprivileged user and can only write to its own folder.
- **Sliced files are checked twice:** by the worker and by the Pi, which accepts only a 3MF with the plate's G-code.
- **Plain HTTP on your network,** like the rest of the dashboard. Keep the Pi and the workers on a private network or a VPN such as Tailscale.

## Licences

- **OrcaSlicer** is licensed under the GNU Affero General Public License v3. This app does not include or change OrcaSlicer. The installer downloads the unmodified official release onto each worker, and the worker runs it as a separate program. OrcaSlicer's presets are read on the worker from that installation.
- **three.js** (the 3D view, in `static/three.module.min.js`, `OrbitControls.js`, `TransformControls.js` and `STLLoader.js`) is MIT licensed, © 2010-2024 three.js authors. See `static/THREE_LICENSE.txt`.
