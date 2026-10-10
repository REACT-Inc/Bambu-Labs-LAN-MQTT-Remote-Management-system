# Live dashboard access from Codex and Claude

`dashboard_mcp.py` is an MCP stdio server for Codex, Claude, and other MCP clients.
It connects to the running management dashboard, using its existing password,
session authentication, CSRF checks, camera endpoint, and printer controls.
It does not expose a new public web server or require third-party Python packages.

Tools:

* `printer_status`: current printer state, connectivity, last-report timestamp,
  demo flag, error presence, progress, remaining time, and temperatures. Settings,
  credentials, activity records, job filenames, and raw telemetry are omitted.
* `printer_camera`: a JPEG snapshot returned as an MCP image for one exact printer
  name. Camera availability depends on the printer and its LAN connection.
* `printer_control`: pause/resume/stop, lights, nozzle/bed/chamber targets, speed,
  fans, or a small relative move on one printer. Every command requires
  `confirmed: true` following the user's authorization for that exact action.
  Movement also requires `homed_clear: true` after the operator verifies homing
  and physical clearance. X/Y are limited to ±10 mm, Z to ±1 mm, minimum 0.1 mm.
  Server checks still require fresh idle telemetry, no error or unresolved queue
  job, and three seconds between moves. No automatic homing or command retries.

## Connect a client

The computer running the MCP server must reach the dashboard. Use HTTPS with a
trusted certificate for remote connections; plain HTTP is accepted only on
loopback. The management app serves HTTP, so configure a trusted HTTPS reverse
proxy or a local tunnel if needed. Redirects and URL-embedded credentials are
rejected, and TLS verification remains enabled.

Create a private file with the dashboard password, accessible only by the MCP
runtime account. Configure these environment variables in your MCP client:

```text
PM_DASHBOARD_URL=https://your-dashboard.example
PM_DASHBOARD_PASSWORD_FILE=/private/dashboard-password.txt
```

For Claude Desktop, merge a server entry into its existing MCP configuration:

```json
{
  "mcpServers": {
    "printer-dashboard": {
      "command": "/absolute/path/to/python3",
      "args": ["/absolute/path/to/dashboard_mcp.py"],
      "env": {
        "PM_DASHBOARD_URL": "https://your-dashboard.example",
        "PM_DASHBOARD_PASSWORD_FILE": "/private/dashboard-password.txt"
      }
    }
  }
}
```

For a Codex installation supporting local MCP servers, use the equivalent entry
in its MCP settings or TOML configuration:

```toml
[mcp_servers.printer_dashboard]
command = "/absolute/path/to/python3"
args = ["/absolute/path/to/dashboard_mcp.py"]

[mcp_servers.printer_dashboard.env]
PM_DASHBOARD_URL = "https://your-dashboard.example"
PM_DASHBOARD_PASSWORD_FILE = "/private/dashboard-password.txt"
```

Use the appropriate Python executable and filesystem paths on Windows. Local
stdio MCP support varies by Codex/Claude product; a PR alone does not install
the server into a chat or establish access to a private network. The Codex
desktop environment in use must also allow the dashboard destination. Do not
put the actual password in client configuration, chat, or Git. The dashboard
password is administrative; keep this integration restricted to trusted users.

After configuring, first request `printer_status`. Verify `demo: false`, the exact
printer name, `connected`, and a fresh `last_seen` timestamp. Request a camera
image before any authorized action that needs visual inspection. A camera image
cannot prove homing or physical clearance; the operator must check those.
Ask for a specific printer, axis, and distance when requesting movement.
Submitted commands are not verified physical results. Check the next status,
camera, and dashboard Activity. A timeout may occur after a command was accepted;
inspect before issuing it again.

## Command-line access

`dashboard_client.py` exposes the same operations for shell tools without MCP:

```bash
python3 dashboard_client.py --url http://127.0.0.1:8080 \
  --password-file /private/dashboard-password.txt status

python3 dashboard_client.py --url http://127.0.0.1:8080 \
  --password-file /private/dashboard-password.txt snapshot \
  --printer 'Printer A' --output /private/printer-camera.jpg

python3 dashboard_client.py --url http://127.0.0.1:8080 \
  --password-file /private/dashboard-password.txt control \
  --printer 'Printer A' --action move --axis X --value 1 \
  --confirm --homed-clear
```

The movement example is for an already authorized, homed, physically clear idle
printer. Other numeric controls use `--value`; speed uses its named mode. Sessions
are held in memory and logged out after each operation. No automatic deployment,
printer movement, credential provisioning, or client configuration is performed
by installing these source files. Runtime camera files may contain private images;
keep them outside Git.
