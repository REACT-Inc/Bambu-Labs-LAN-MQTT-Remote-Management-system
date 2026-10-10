# Printer status bar

The management service provides `GET /api/printer-status` using the existing
dashboard session authentication. It returns only `summary`, `detail`, and
`tooltip` strings. It uses current printer telemetry, display names, progress,
and estimated minutes remaining. It excludes printer addresses, serial numbers,
access codes, job filenames, raw telemetry, account details, and configuration.
Demo mode is marked in the summary. Disconnected printers or reports older than
90 seconds are offline; previous progress is not shown as current.

## Local status file

Run `status_client.py` with Python 3 on the computer displaying the bar. It has
no third-party dependencies. Create a private password file containing the
dashboard password, readable only by the account running the client. This
password grants dashboard administrative access: do not distribute it to
ordinary users. Run the client under a trusted account and grant display users
read access only to the output file, never to the password file.

Example when the client runs on the management host:

```bash
python3 status_client.py --url http://127.0.0.1:8080 \
  --password-file /private/dashboard-password.txt \
  --output /private/printer-status.json
```

For a remote computer use an HTTPS origin with a trusted certificate, including
its port if needed. Remote plain HTTP and redirects are rejected. TLS verification
is always enabled. Configure a trusted HTTPS reverse proxy if one is not already
available; the application itself serves HTTP. No printer credentials are needed
on the display computer. The client keeps the authenticated session in memory
and signs in again when the session expires. Protect its runtime account.

The client refreshes every 15 seconds and replaces the JSON file atomically.
On authentication, network, or response failure it writes “Printer status
unavailable” instead of retaining old success text. `--once` performs a single
refresh and exits nonzero on failure. If the client stops, the last file remains;
supervise/restart it and monitor its output modification time. No startup task,
file permissions, or deployment changes are installed automatically.

## YASB widget

Add a custom widget to your own YASB configuration and include `printers` in the
desired bar widget list. This standalone example assumes the client output is
`C:\PrinterStatus\status.json`; use your own local path and file permissions.

```yaml
printers:
  type: "yasb.custom.CustomWidget"
  options:
    label: "Printers: {data[summary]}"
    label_alt: "{data[detail]}"
    class_name: "printers-widget"
    tooltip: true
    tooltip_label: "{data[tooltip]}"
    exec_options:
      run_cmd: 'cmd.exe /c type C:\PrinterStatus\status.json'
      run_interval: 15000
      return_format: "json"
      hide_empty: false
    callbacks:
      on_left: "toggle_label"
      on_middle: "do_nothing"
      on_right: "do_nothing"
```

The regular label shows fleet counts, clicking toggles per-printer detail, and
hovering shows one printer per line. Start the client before loading the widget.
Use generic display aliases if printer names are sensitive. Status output is
runtime data; never commit it or password files to a repository.
