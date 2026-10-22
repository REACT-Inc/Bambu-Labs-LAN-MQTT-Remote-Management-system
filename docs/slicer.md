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

### 1. Prepare the computer

Any x86_64 (Intel or AMD) laptop or PC will do. ARM computers such as a Raspberry Pi won't work, because OrcaSlicer only publishes Linux builds for x86_64.

- **Linux:** **Ubuntu Server 24.04 LTS** (recommended), or Debian 13. A desktop isn't needed. OrcaSlicer's Linux build needs these newer system libraries, so on Ubuntu 22.04 it only works if the OrcaSlicer release has a 22.04 build. The installer's test slice tells you.
- **Memory and disk:** 4 GB of RAM or more (8 GB for big plates), and about 3 GB of free disk.
- **Network:** the same network as the Pi (or the same VPN, such as Tailscale). The worker only connects out to the Pi and to GitHub, so no ports need opening.
- **When installing Ubuntu Server:** choose **Install OpenSSH server** so you can run the rest from another computer. Then sign in and run `sudo apt update && sudo apt install -y curl`.

### 2. Run the installer

1. In the dashboard, open **Slicer → Workers → Add a worker**. You get a command with a one-time setup code that is valid for 24 hours.
2. Run that command on the worker computer:

   ```bash
   curl -fsSL http://PI-ADDRESS:8080/slicer/worker/install.sh | sudo bash -s -- --server http://PI-ADDRESS:8080 --code SETUP-CODE
   ```

   Without `--server` or `--code`, it asks for them. Add options to the end of the command:

   | Option | What it does |
   |---|---|
   | `--name "Laptop 3"` | The name shown in the dashboard (default: the computer's hostname) |
   | `--memory-max 12G` | The most memory the worker may use (default 6G). Leave about 2 GB for the system. |
   | `--auto-update` | Updates the worker from the Pi every night at about 04:30, only while it isn't slicing |
   | `--no-keep-awake` | Doesn't stop a laptop from sleeping when its lid is closed |
   | `--orca-url URL` | Downloads the OrcaSlicer Linux AppImage from this link instead of GitHub |
   | `--yes` | Never asks anything, for scripted installs |

It takes a few minutes and shows each step:

1. **Checks the computer:** x86_64, Ubuntu or Debian version, memory, disk, and that it can reach the dashboard.
2. **Installs the packages** OrcaSlicer needs. These include `xvfb`, because OrcaSlicer's command line still needs a virtual display.
3. **Downloads OrcaSlicer 2.4.2** from its official GitHub release (the Linux AppImage) and unpacks it in `/opt/orcaslicer`.
4. **Downloads the worker** from the Pi into `/opt/orca-worker`, and adds the `slicer-worker` command.
5. **Registers** with the setup code. The worker's token is kept in `/etc/orca-worker/config.json`, readable only by root and the worker.
6. **Slices a 20 mm test cube** to prove OrcaSlicer works on that computer.
7. **Installs the `orca-worker` service.** It starts on boot, runs as its own `orcaworker` user, has its memory limited, can only write to `/var/lib/orca-worker`, and is restarted if it stops.
8. **Keeps a laptop awake:** it ignores the lid switch and turns off sleep and suspend, so the worker keeps going with the lid closed.

The worker then shows under **Slicer → Workers**, and its presets appear in the Slicer tab within a few seconds.

### 3. Managing a worker: `slicer-worker`

| Command | What it does |
|---|---|
| `sudo slicer-worker status` | Running or not, registration, OrcaSlicer and worker versions, slicing now, memory limit, the last log lines |
| `sudo slicer-worker logs` | Follows the worker's log (Ctrl+C stops it) |
| `sudo slicer-worker test` | Slices the test cube again |
| `sudo slicer-worker doctor` | Checks the system, memory, disk, the dashboard, GitHub, OrcaSlicer's libraries and the service, and says what to fix |
| `sudo slicer-worker update` | Updates the worker from the Pi and, when this app moves to a newer OrcaSlicer version, OrcaSlicer too. Keeps the computer's options (memory limit, keep-awake, nightly updates). |
| `sudo slicer-worker restart` | Restarts the service |
| `sudo slicer-worker uninstall` | Removes the service, the worker and OrcaSlicer, and lets the laptop sleep again. `--purge` also removes its registration and data. |

**Moving the Pi to a new address:** `sudo slicer-worker update --server http://NEW-ADDRESS:8080`.

**Removing a worker:** `sudo slicer-worker uninstall --purge` on the computer, then **Remove** in the dashboard.

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
| Worker shows 🔴 offline | On the worker: `sudo slicer-worker doctor`, then `sudo slicer-worker logs`. Check it can reach the Pi's address and port; if the Pi moved, `sudo slicer-worker update --server http://NEW-ADDRESS:8080`. |
| The installer's test slice fails | Its output shows OrcaSlicer's error. Run `sudo slicer-worker doctor`: it lists missing libraries. Usually `sudo slicer-worker update` fixes it; on unusual distributions install `libwebkit2gtk-4.1-0` and `libgtk-3-0` by hand. |
| *Release file … is not valid yet* during the install | The computer's clock is wrong. Newer installers fix it by themselves; otherwise run `sudo timedatectl set-ntp true` and try again. |
| A laptop worker goes offline when its lid is closed | It was installed with `--no-keep-awake`. Run the installer again without it. |
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
