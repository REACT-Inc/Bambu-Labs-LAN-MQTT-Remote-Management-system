# Google sign-in at printers.your-domain (Cloudflare Access)

Open the dashboard at your own address, for example `https://printers.example.org`, from anywhere, behind a Google sign-in. No port is opened on your router, and the Pi needs no certificate.

- **Cloudflare Tunnel:** `cloudflared` runs on the Pi and connects out to Cloudflare. Cloudflare then serves the dashboard at `printers.<your domain>` over HTTPS.
- **Cloudflare Access:** before anything reaches the Pi, Access asks for a Google sign-in and checks it against your rules (for example, only these people, or anyone `@example.org`).
- **The dashboard checks it too:** every request carries Cloudflare's signed token. The dashboard checks the token is signed by your Cloudflare team for your Access application, and reads the Google account from it. Requests without a valid token get nothing.
- **Three levels, by Google account:**
  - **Admins** can do everything, including management: Team tools (Discord), Laptops, Server, Settings & help, and adding or changing slicing workers.
  - **Who may print** (operators) can use the printers, the queue, the slicer and Activity. With nobody listed, everyone Access lets in can.
  - **Everyone else Access lets in can only look:** they can watch the printers, cameras, queue and Activity, but can't queue, slice, control a printer or change anything.

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
sudo bash cloudflare-tunnel.sh
```

**What it asks for** (or give them as options):
- **The tunnel token** (`--token`).
- **Your domain** (`--domain example.org`): the dashboard goes at `printers.example.org`. For another name, use `--hostname`.
- **The team** (`--team yourteam`, or the full `yourteam.cloudflareaccess.com`).
- **The AUD tag** (`--aud`).
- **The admins** (`--admin you@example.org --admin @staff.example.org`).
- **What the Pi's own addresses may use** (`--lan printing`).

**Then it:**
1. Checks the settings, so a typo stops it before anything changes.
2. Installs `cloudflared`, from Cloudflare's package repository on Raspberry Pi OS, Debian and Ubuntu, or from its GitHub releases on other systems.
3. Connects your tunnel as the `cloudflared` service.
4. Saves the settings and restarts the dashboard.
5. Opens `https://printers.example.org` like a visitor would, and tells you whether it really asks for a Google sign-in. It warns you if the address answers without one, uses another team, or can't reach the Pi.

**Or set up the sign-in from the dashboard:** connect only the tunnel with `sudo bash cloudflare-tunnel.sh --tunnel-only --token '<token>'`. Then fill in **Settings & help → Google sign-in (Cloudflare Access)** and press **Test** before saving.
- **Test** checks that the Pi can read your team's signing keys, and that the address asks for a Google sign-in.
- **Saving applies at once,** with no restart.
- **The dashboard won't lock you out without asking:** saving asks first when it would take management away from the address you're using, or remove you from the admins.

Then open `https://printers.example.org`. Cloudflare asks you to sign in with Google, then the dashboard opens.
- **The sidebar** shows who you're signed in as, with **Sign out**.
- **Activity** records changes under the Google account that made them, instead of "web administrator".
- **Recent sign-ins:** at the bottom of **Settings & help → Google sign-in**, admins see which Google accounts used the dashboard. The list shows when each account was last there and first seen, and its IP address as Cloudflare saw it.
  - **Change one person's level right there:** **Admin**, **Can print**, **View only** or **Blocked**. A person's own level wins over the lists. **From the lists** goes back to them.
  - **Blocked accounts are refused at once,** even with a valid Cloudflare sign-in (removing someone from the Access policy only takes effect when their sign-in expires, up to a day later). They see "This Google account has been blocked".
  - **Your own level:** another admin has to change it, so you can't lock yourself out.
  - **Saved as `"people"`:** for example `{"kid@example.org": "view"}` in the settings file.
- **View-only accounts** see a banner saying so, and no **+ Queue a print** button.
- **Ask to print:** the view-only banner has an **Ask to print** button.
  - **Admins** see a count on **Settings & help** (on **More** on phones).
  - **In Recent sign-ins,** the request is at the top, with **Allow** and **Dismiss**. **Allow** gives that person **Can print** at once.
- **Recent sign-ins also mark** who's **online now** (in the last 5 minutes) and accounts first seen in the last day (**new**).
- **When a Google sign-in expires** while the page is open, a **Sign in again** banner appears. Pressing it reloads the page through Google.

**Admins and who may print:** each entry is a Google address, or `@example.org` for everyone in that domain.
- **With no admins,** every account your Access policy allows is an admin.
- **With nobody under Who may print,** every account can print.
- **Example for a school:** Access lets in `@school.org`, Who may print lists `@club.school.org`, and the admins are the two coaches. Then the whole school can watch, the club can print, and the coaches manage.
- **In the script:** `--operator`, the same as `--admin`.

**Where the settings are saved:** `/var/lib/3d-printer-management/cloudflare_access.json`, by both the script and Settings. You can also put the same settings under `"cloudflare_access"` in `config.json`, which is used only when that file doesn't exist:

```json
"cloudflare_access": {
  "team_domain": "yourteam.cloudflareaccess.com",
  "aud": "4714c1358e65fe4b408ad6d432a5f878...",
  "hostname": "printers.example.org",
  "admins": ["you@example.org", "@staff.example.org"],
  "lan": "printing"
}
```

**Checked every 5 minutes:** the dashboard checks that it can still read Cloudflare's signing keys and that the `cloudflared` service is running. If either fails, the status icon (top left) says so.

## The Pi's own addresses

The dashboard still answers on the Pi's own addresses, such as `http://192.168.1.50:8080`, `.local` names and Tailscale. These requests don't go through Cloudflare, so there's no Google account to check. `"lan"` decides what they may do:

| `lan` | The Pi's own addresses can |
|---|---|
| `printing` (default) | Use the printers, the queue, the slicer and Activity. Management is only at `https://printers.<your domain>`. |
| `full` | Everything, management included (as before Cloudflare Access was set up). |
| `view` | Look only: the printers, cameras, queue and Activity, with no changes. |
| `off` | Nothing: the dashboard is only at `https://printers.<your domain>`. |

Set it in **Settings & help → Google sign-in**, or with `--lan` when you run the script.

**Cloudflare's requests can't pose as local ones:** any request that has the public hostname, or Cloudflare's own headers (`Cf-Ray`, `Cf-Connecting-Ip`), must carry a valid Access token, whatever `lan` says.

## Slicing workers and the desktop app

- **Slicing workers:** they sign in with their own token, not Google, so connect them to the Pi's own address. For example, `--server http://192.168.1.50:8080/` in the worker install command.
  - **If a worker can only reach the public hostname:** add a second **Self-hosted** Access application for `printers.<your domain>` with the paths `api/slicer/worker` and `slicer/worker`, and a policy with action **Bypass** that includes **Everyone**. The worker routes still need the worker's token.
- **Desktop app (Windows):** connect it to the Pi's own address, and keep `"lan"` at anything but `off`, or the app can't connect. Google doesn't allow signing in from inside other apps' windows, so the public address may not work there. With `"lan": "printing"` the app can print. Use a browser at `https://printers.<your domain>` for management.

## Changing or removing it

- **Add or remove admins:** use **Settings & help → Google sign-in** (applies at once).
- **Who may sign in at all:** change the Access application's policy in Zero Trust. Changes apply at once and need nothing on the Pi.
- **Turn it off:**
  - **Turn off** in **Settings & help → Google sign-in** turns the sign-in off.
  - `sudo bash cloudflare-tunnel.sh --remove` also stops the tunnel.
  - Then delete the tunnel and the Access application in Zero Trust.

## Troubleshooting

| Problem | What to check |
|---|---|
| **"Your Google sign-in has expired" banner, again right after Sign in again** | The Access sign-in expired, or the token doesn't match the settings. Check that `aud` is this application's Audience tag and `team_domain` is your team. The Pi must also reach `https://<team>.cloudflareaccess.com` (it downloads Cloudflare's keys from there) and have the right time (`timedatectl`). |
| **Cloudflare shows error 1033 or 502** | The tunnel isn't connected (`sudo systemctl status cloudflared`), or its public hostname doesn't point at `http://localhost:8080`. |
| **"Your Google account (…) isn't a dashboard admin"** | An admin adds it in **Settings & help → Google sign-in**. |
| **The status icon says "Google sign-in can't check sign-ins"** | The Pi can't download your team's keys from `https://<team>.cloudflareaccess.com`. Check its internet connection, DNS, and the team domain. Nobody can use the public address until it can. |
| **Locked out of management** | On the Pi, `sudo bash cloudflare-tunnel.sh --remove` (or write `{}` into `/var/lib/3d-printer-management/cloudflare_access.json` and restart the dashboard) turns the sign-in off. |
| **"Management is open at https://printers…"** | You're on one of the Pi's own addresses with `"lan": "printing"`. Use the public address, or set `"lan": "full"`. |
| **Google says the app isn't verified, or refuses the account** | On Google's OAuth consent screen, use **Internal** for Workspace accounts, or add the people as test users while the app is in testing. |
