# Google sign-in at printers.your-domain (Cloudflare Access)

Open the dashboard at your own address, for example `https://printers.example.org`, from anywhere, behind a Google sign-in. No port is opened on your router, and the Pi needs no certificate.

- **Cloudflare Tunnel:** `cloudflared` runs on the Pi and connects out to Cloudflare. Cloudflare then serves the dashboard at `printers.<your domain>` over HTTPS.
- **Cloudflare Access:** before anything reaches the Pi, Access asks for a Google sign-in and checks it against your rules (for example, only these people, or anyone `@example.org`).
- **The dashboard checks it too:** every request carries Cloudflare's signed token. The dashboard checks the token is signed by your Cloudflare team for your Access application, and reads the Google account from it. Requests without a valid token get nothing.
- **Admins manage, everyone else prints.** Google accounts listed as admins can use management: Team tools (Discord), Laptops, Server, Settings & help, and adding or changing slicing workers. Everyone else Access lets in can use the printers, the queue, the slicer and the activity feed.

This replaces the dashboard password for management. The password is still used by the [pm-doctor page](doctor.md), and by the dashboard if you turn on `"require_login": true`.

## Contents

- [What you need](#what-you-need)
- [1. Add Google as a login method](#1-add-google-as-a-login-method)
- [2. Create the tunnel](#2-create-the-tunnel)
- [3. Create the Access application](#3-create-the-access-application)
- [4. Connect the Pi](#4-connect-the-pi)
- [The Pi's own addresses](#the-pis-own-addresses)
- [Slicing workers and the desktop app](#slicing-workers-and-the-desktop-app)
- [Changing or removing it](#changing-or-removing-it)
- [Troubleshooting](#troubleshooting)

## What you need

- **A domain on Cloudflare:** your domain must use Cloudflare's nameservers (Cloudflare dashboard → **Add a domain**). The free plan is enough.
- **Cloudflare Zero Trust:** in the Cloudflare dashboard, open **Zero Trust** and pick a team name. Your team domain is `<team name>.cloudflareaccess.com`. The free plan covers up to 50 users.
- **A Google Cloud project,** for the Google sign-in. Any Google account can make one at [console.cloud.google.com](https://console.cloud.google.com).
- **The dashboard installed on the Pi,** version 1.9.21 or later.

## 1. Add Google as a login method

1. **Create an OAuth client in Google Cloud:**
   1. Go to **APIs & Services → OAuth consent screen**, and set up the consent screen. Choose **Internal** to allow only your Google Workspace, or **External** for any Google account.
   2. Go to **APIs & Services → Credentials → Create credentials → OAuth client ID**, and pick **Web application**.
   3. Under **Authorized JavaScript origins**, add `https://<team name>.cloudflareaccess.com`.
   4. Under **Authorized redirect URIs**, add `https://<team name>.cloudflareaccess.com/cdn-cgi/access/callback`.
   5. Copy the **Client ID** and **Client secret**.
2. **Add Google in Zero Trust:** go to **Settings → Authentication → Login methods → Add new → Google**, paste the client ID and secret, and save. **Test** should show your Google account.

## 2. Create the tunnel

1. In Zero Trust, go to **Networks → Tunnels → Create a tunnel**, choose **Cloudflared**, and name it (for example `print-room-pi`).
2. Cloudflare shows install commands with a long **token** after `service install`. Copy only the token: the script in [step 4](#4-connect-the-pi) runs the install for you.
3. **Add the public hostname:**
   - **Subdomain:** `printers`.
   - **Domain:** your domain.
   - **Service:** type **HTTP**, URL `localhost:8080`. Use the dashboard's port if you changed `"port"` in `config.json`.

## 3. Create the Access application

1. In Zero Trust, go to **Access → Applications → Add an application → Self-hosted**.
2. **Application domain:** subdomain `printers`, and your domain.
3. **Identity providers:** pick **Google** only. To skip Cloudflare's own login screen, also turn on **Instant Auth**.
4. **Add a policy,** with action **Allow**. Include whoever may use the dashboard:
   - **Emails:** each person's Google address.
   - **Emails ending in:** `@example.org`, for a whole Google Workspace.
5. **Save,** then open the application again. Under its settings (Overview / Basic information), copy the **Application Audience (AUD) Tag**: a long hex string.

## 4. Connect the Pi

From the extracted release folder (the one with `install.sh`):

```bash
sudo bash cloudflare-tunnel.sh \
  --token '<tunnel token from step 2>' \
  --hostname printers.example.org \
  --team <team name>.cloudflareaccess.com \
  --aud '<Application Audience tag from step 3>' \
  --admin you@example.org --admin teacher@example.org
```

The script:
1. Installs `cloudflared`, from Cloudflare's package repository on Raspberry Pi OS, Debian and Ubuntu, or from its GitHub releases on other systems.
2. Connects your tunnel as the `cloudflared` service.
3. Saves the settings under `"cloudflare_access"` in `/etc/3d-printer-management/config.json`.
4. Restarts the dashboard.

Then open `https://printers.example.org`. Cloudflare asks you to sign in with Google, then the dashboard opens. **Settings & help → Dashboard access** shows who you're signed in as, whether you're an admin, and **Sign out of Google**.

**`--admin`:** each one is a Google address, or `@example.org` for everyone in that domain. Leave them all out and every account your Access policy allows is an admin.

What the script saves in `config.json` (you can also edit it by hand, then `sudo systemctl restart 3d-printer-management`):

```json
"cloudflare_access": {
  "team_domain": "yourteam.cloudflareaccess.com",
  "aud": "4714c1358e65fe4b408ad6d432a5f878...",
  "hostname": "printers.example.org",
  "admins": ["you@example.org", "@staff.example.org"],
  "lan": "printing"
}
```

## The Pi's own addresses

The dashboard still answers on the Pi's own addresses, such as `http://192.168.1.50:8080`, `.local` names and Tailscale. These requests don't go through Cloudflare, so there's no Google account to check. `"lan"` decides what they may do:

| `lan` | The Pi's own addresses can |
|---|---|
| `printing` (default) | Use the printers, the queue, the slicer and Activity. Management is only at `https://printers.<your domain>`. |
| `full` | Everything, management included (as before Cloudflare Access was set up). |
| `off` | Nothing: the dashboard is only at `https://printers.<your domain>`. |

Set it with `--lan` when you run the script, or in `config.json`.

**Cloudflare's requests can't pose as local ones:** any request that has the public hostname, or Cloudflare's own headers (`Cf-Ray`, `Cf-Connecting-Ip`), must carry a valid Access token, whatever `lan` says.

## Slicing workers and the desktop app

- **Slicing workers:** they sign in with their own token, not Google, so connect them to the Pi's own address. For example, `--server http://192.168.1.50:8080/` in the worker install command.
  - **If a worker can only reach the public hostname:** add a second **Self-hosted** Access application for `printers.<your domain>` with the paths `api/slicer/worker` and `slicer/worker`, and a policy with action **Bypass** that includes **Everyone**. The worker routes still need the worker's token.
- **Desktop app (Windows):** connect it to the Pi's own address. Google doesn't allow signing in from inside other apps' windows, so the public address may not work there. With `"lan": "printing"` the app can print. Use a browser at `https://printers.<your domain>` for management.

## Changing or removing it

- **Add or remove admins:** edit `"admins"` in `config.json` and restart, or run the script again with the new `--admin` list.
- **Who may sign in at all:** change the Access application's policy in Zero Trust. Changes apply at once and need nothing on the Pi.
- **Turn it off:** `sudo bash cloudflare-tunnel.sh --remove` stops the tunnel and removes `"cloudflare_access"`. Then delete the tunnel and the Access application in Zero Trust.

## Troubleshooting

| Problem | What to check |
|---|---|
| **"Sign in with Google again: reload the page."** | The Access sign-in expired, or the token doesn't match the settings. Check that `aud` is this application's Audience tag and `team_domain` is your team. The Pi must also reach `https://<team>.cloudflareaccess.com` (it downloads Cloudflare's keys from there) and have the right time (`timedatectl`). |
| **Cloudflare shows error 1033 or 502** | The tunnel isn't connected (`sudo systemctl status cloudflared`), or its public hostname doesn't point at `http://localhost:8080`. |
| **"Your Google account (…) isn't a dashboard admin"** | Add it to `"admins"`, then restart. |
| **"Management is open at https://printers…"** | You're on one of the Pi's own addresses with `"lan": "printing"`. Use the public address, or set `"lan": "full"`. |
| **Google says the app isn't verified, or refuses the account** | On Google's OAuth consent screen, use **Internal** for Workspace accounts, or add the people as test users while the app is in testing. |
