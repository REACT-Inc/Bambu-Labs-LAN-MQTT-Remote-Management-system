# Laptops (MeshCentral)

Shows the team's Windows laptops and runs a small set of **approved commands** on them, from Discord or the dashboard's **Laptops** tab. It uses your existing **[MeshCentral](https://meshcentral.com/)** server and the MeshCentral agents already on the laptops. No other laptop software is needed.

Use MeshCentral itself for remote desktop, enrolling devices and anything advanced.

- [Connecting MeshCentral](#connecting-meshcentral)
- [Which laptops appear](#which-laptops-appear)
- [Running commands](#running-commands)
- [Adding approved commands](#adding-approved-commands)
- [Understanding results](#understanding-results)
- [Upgrading from the old Python laptop agent](#upgrading-from-the-old-python-laptop-agent)

## Connecting MeshCentral

1. **Create a login token** in MeshCentral (**My Account → Login tokens**) for an account that can see the laptops' device groups and run commands on them. Use the token's generated username and password. A normal password with interactive two-factor authentication won't work.
2. **Open the connection settings:** dashboard **Laptops → MeshCentral connection**.
3. **Fill in the connection:**

   | Field | Example |
   |---|---|
   | HTTPS server URL | `https://mesh.example.org` (include a domain path if your server uses one) |
   | Login-token username / password | from step 1 |
   | Laptop name prefix | `REACT-` |

4. **Save & connect.**

Security details:
- **Reachability:** the Pi must be able to reach the URL.
- **Certificates:** TLS certificates are always verified; there's no option to turn this off. For a private certificate authority, add `"ca_file": "/absolute/path/ca.pem"` to `/var/lib/3d-printer-management/meshcentral.json`, make the file readable by `printermanager`, and restart the service.
- **Token storage:** the token is stored in `meshcentral.json` (mode 600) and never sent back to the browser.
- **Changing settings:** leaving the password blank keeps the saved token, but only for the same server and user.

## Which laptops appear

A laptop is listed when all of these hold:
- its MeshCentral **name starts with the prefix** (for example `REACT-`)
- the token's account can see it
- it's in one of the listed device groups, if you add `"mesh_ids": ["mesh//…"]` to `meshcentral.json` to limit the groups

Commands can only be sent to **online Windows** laptops.

## Running commands

| Where | How |
|---|---|
| Discord | `/laptops` refreshes the list. `/laptop cmd device_id command` runs a command, with autocomplete (run `/laptops` first if it's empty). `/laptop result task_id` shows a result. All are admin-only. |
| Dashboard | **Laptops** tab: device cards with the approved commands, plus a results feed |

- **Confirmation:** every command asks for confirmation.
- **Command names only:** you pick a command *name*; you can never type shell commands or arguments.
- **Where it runs:** in the MeshCentral agent's context, which is normally SYSTEM on Windows, not the logged-in user's desktop.

## Adding approved commands

The defaults are:

| Name | Runs |
|---|---|
| `hostname` | `hostname` |
| `network` | `ipconfig /all` |
| `whoami` | `whoami` |
| `uptime` | PowerShell: time since last boot |

To change them, edit the `commands` object in `/var/lib/3d-printer-management/meshcentral.json` and restart the service:

```json
"commands": {
  "hostname": {"script": "hostname", "type": 0},
  "network":  {"script": "ipconfig /all", "type": 0},
  "uptime":   {"script": "(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime", "type": 2}
}
```

`type` `0` uses the agent's default shell, and `type` `2` uses PowerShell.

## Understanding results

| Status | Meaning |
|---|---|
| `response` | MeshCentral returned output. This doesn't necessarily mean the command succeeded. |
| `submitted` | MeshCentral acknowledged the command; no output yet |
| `unknown` | Delivery or the result couldn't be confirmed. Check in MeshCentral before trying again. |

- **No retries:** commands are never retried automatically or queued for offline laptops.
- **Missing results:** if a server doesn't send the expected replies, the status stays `unknown`; no result is invented.

## Upgrading from the old Python laptop agent

The old custom laptop agent has been retired:
- **Endpoints removed:** its endpoints (`/agent/heartbeat`, `/agent/result`) and the enrolment controls are gone.
- **Old tasks:** its pending tasks aren't resent.

**On each laptop:** turn off the old agent's startup entry, and keep the MeshCentral agent running. No files are deleted from laptops.

---

This uses MeshCentral's control WebSocket API: the `nodes` and `runcommands` operations, with `x-meshauth` login and per-request `responseid` matching. See [meshctrl.js](https://github.com/Ylianst/MeshCentral/blob/master/meshctrl.js) and the [meshctrl docs](https://docs.meshcentral.com/meshctrl/).

**Not yet tested on a real server:** it's been tested against simulated MeshCentral responses only.
