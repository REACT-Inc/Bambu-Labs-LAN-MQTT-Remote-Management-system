# Laptop management through MeshCentral

The Python laptop agent has been retired. Devices, online state and commands now
come from your existing MeshCentral server and its installed MeshCentral agents.
The old /agent/heartbeat and /agent/result endpoints and custom enrollment controls
are removed. Existing custom-agent tasks will not be resent through MeshCentral.
Disable the old Python agent's startup entry on any laptop where you installed it;
keep its MeshCentral agent running. Existing files on laptops are not deleted.

## Connect

1. In MeshCentral, create an account login token with access to the intended device
   groups and permission to run commands. Use the token's generated username and
   password. A normal password requiring interactive MFA is not suitable here.
2. Open the Pi dashboard → Laptops → MeshCentral connection.
3. Enter your actual HTTPS MeshCentral base URL (include its domain path if used),
   token username/password, and laptop prefix, normally REACT-.
4. Save & connect. No additional laptop script or FlowQ software is needed.

The Pi must be able to reach the URL. TLS certificates are verified. For a private
CA, add `ca_file` with the absolute path to a trusted PEM CA file in
`/var/lib/3d-printer-management/meshcentral.json`, readable by printermanager,
then restart the management service. There is no disable-certificate-check option.
The token is stored mode 0600 in that JSON file and never returned by the API.
An empty password field retains the existing token only for the same server/user.

## Commands

`/laptops` refreshes the device list. `/laptop cmd` offers cached device and command
autocomplete; run `/laptops` first if it is empty. Only an online Windows laptop
matching the configured prefix (and optional `mesh_ids` list in the config) can
receive commands. Access is further limited by the MeshCentral token's rights.
Commands require confirmation and Discord admin/approved-ID permission. The
password-authenticated Pi dashboard is also an administrative interface.

Defaults: hostname, network (`ipconfig /all`), whoami, and PowerShell uptime.
To add approved commands, edit the `commands` object in meshcentral.json on the Pi:

```json
"commands": {
  "hostname": {"script": "hostname", "type": 0},
  "network": {"script": "ipconfig /all", "type": 0},
  "uptime": {"script": "(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime", "type": 2}
}
```

Type 0 uses the agent's default shell, type 2 uses PowerShell. Commands run in the
MeshCentral agent context (normally SYSTEM on Windows). This is not an interactive
logged-in user's desktop. The UI accepts command names only, never arbitrary shell
arguments. Restart the service after editing this file. Use MeshCentral itself
for remote desktop, enrollment and advanced device management.

Results appear in the dashboard or `/laptop result task_id:...`. `response` means
MeshCentral returned text, not necessarily a successful exit code. `submitted`
means only acknowledgement was received. `unknown` means delivery or the result
could not be confirmed; check MeshCentral before retrying. No automatic retries
or offline command queueing are performed. Servers without the expected reply
support may show unknown; no result is invented.

Protocol reference: the official MeshCentral meshctrl.js nodes and runcommands
operations, including x-meshauth and per-request responseid correlation:
https://github.com/Ylianst/MeshCentral/blob/master/meshctrl.js
https://docs.meshcentral.com/meshctrl/

This integration was tested against mocked protocol responses. It has not been
connected to your MeshCentral server in this workspace.
