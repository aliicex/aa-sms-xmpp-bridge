# SMS to XMPP Bridge (Proxmox)

This project provides a Community Script for Proxmox VE that deploys a lightweight Debian LXC container. The container runs an asynchronous Python bot (`slixmpp` + `aiohttp`) and `cloudflared` to act as a secure bidirectional bridge between the Andrews & Arnold (AAISP) SMS API and an XMPP client (such as Cheogram, Conversations, or Gajim).

## Features
- **Inbound SMS:** Received via Cloudflare Tunnel -> Local webhook (`localhost:8080`) -> Pushed to your XMPP chat with zero open firewall ports.
- **Outbound SMS:** Sent via XMPP using flexible UK mobile prefixes (`07...` or `+447...`) or direct sticky replies -> AAISP HTTP POST, with active `y`/`n` confirmation and XMPP error replies if delivery is rejected.
- **Bill Protection:** Previews SMS segment count and estimated cost (`~3.6p`/part), warns on Unicode/emoji encoding, normalizes smart punctuation (`’`, `“`, `—`) to GSM-7, enforces UK mobile (`+447`) destinations only, caps messages at **4 parts**, and limits sending to **30 SMS/hour**.
- **Security:** Secrets are stored in a root-only environment file, webhook tokens are verified in constant time, presence subscriptions are restricted to `TARGET_USER_JID`, and OMEMO-encrypted stanzas are flagged with a plain-text reminder.
- **Reliability:** Built with `asyncio` and graceful `SIGTERM` handling to cleanly disconnect during container updates.

## 1. Create a Cloudflare Tunnel (Recommended)

Because `cloudflared` runs inside the container, you do **not** need to open or port-forward any ports on your router.

1. Log into the [Cloudflare Zero Trust Dashboard](https://cloudflare.com "Cloudflare Zero Trust Dashboard") and go to **Networks** -> **Tunnels**.
2. Click **Create a tunnel**, choose **Cloudflared**, and give it a name (e.g., `sms-xmpp-bridge`).
3. Copy the **connector token** (`eyJhIjo...`) shown on the installation page—you will paste this during the Proxmox script installation.
4. Open the **Public Hostname** tab for the tunnel and add a route:
   - **Subdomain:** `sms`
   - **Domain:** `yourdomain.com`
   - **Path:** `webhook` *(recommended so only `/webhook` is routed to the container)*
   - **Service Type:** `HTTP`
   - **URL:** `localhost:8080`

*(Note: Ensure you do **not** place a Cloudflare Access authentication policy on `://yourdomain.com`, as AAISP's automated webhook POST requests cannot complete an interactive login.)*

## 2. Installation via Proxmox

Run the following command in your **Proxmox VE host shell**:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/aliicex/aa-sms-xmpp-bridge/main/ct/sms-xmpp-bridge.sh)"
```

During installation:
- A random `WEBHOOK_TOKEN` is automatically generated and printed to the console.
- You will be prompted to paste your **Cloudflare Tunnel token** (or press **Enter** to skip and run `cloudflared service install <TOKEN>` later).

## 3. Configuration & Secrets

The installation script creates a root-only configuration file inside the container. The bot will not connect until the required credentials are populated.

### Populate the Secrets File
Open the container console (or SSH in) and edit `/etc/sms-xmpp-bridge/secrets.env`:

```bash
nano /etc/sms-xmpp-bridge/secrets.env
```

Fill in the values (see `.env.example` for formatting):
- `XMPP_JID`: The JID of the bot account.
- `XMPP_PASSWORD`: The password for the bot account.
- `AA_USERNAME`: Your AAISP VoIP number (e.g., `+44XXXXXXXXXX` or `0XXXXXXXXXX`).
- `AA_PASSWORD`: Your AAISP **VoIP outgoing password** (configured in the AAISP Control Pages, not your main portal login).
- `WEBHOOK_TOKEN`: Pre-populated during installation (keep this value or replace it with your own random string).
- `TARGET_USER_JID`: Your personal XMPP JID (the only JID allowed to send SMS, subscribe to bot presence, and receive notifications).

### Apply Changes & Verify Status
Restart the service to load your credentials and follow the logs:

```bash
systemctl restart sms-xmpp-bridge
journalctl -u sms-xmpp-bridge -f
```

## 4. Setup in AAISP Control Panel

Log into your [AAISP Control Pages](https://aa.net.uk "Andrews & Arnold Control Pages") and open the VoIP settings for your number:

1. **Inbound SMS Target:** Set the webhook URL to:
   ```text
   https://://yourdomain.com?token=YOUR_WEBHOOK_TOKEN
   ```
   *(Use the `WEBHOOK_TOKEN` value from `/etc/sms-xmpp-bridge/secrets.env`.)*
2. **Recommended Spend Limit:** Configure a monthly outbound call/SMS spend cap on the number as an account-level backstop in addition to the bot's built-in rate and segment limits.

## Usage

### Receiving SMS
When an SMS arrives on your AAISP number, the bot forwards the sender's number and message body to `TARGET_USER_JID`, along with a status line showing whether direct sticky replies are active.

*(Note: Ensure OMEMO/PGP encryption is turned off for your bot chat in your XMPP client, as standard SMS is unencrypted.)*

### Sending SMS (UK Mobile Numbers Only)
All UK mobile formats (`07...`, `07700 900000`, `00447...`, or `+447...`) are automatically normalized to `+447...`. Non-UK international prefixes and non-mobile numbers are rejected.

**1. Starting a new conversation:**
Send a message starting with the UK mobile number followed by a space, colon, newline, or pipe:

```text
07700900000 Hello! This is a test message.
07700 900000: Spaced numbers work with a colon or newline.
+447700900000 Hello!
```

**2. Replying to an active conversation (sticky replies):**
After receiving an SMS, simply type your reply without a number within **15 minutes** (e.g., `On my way!`).
- **Active confirmation & cost preview:** Before sending, the bot displays the destination number, SMS part count, estimated cost (`~3.6p`/part), and any Unicode/emoji warning, asking you to reply `y` (within 60 seconds) to confirm or `n` to cancel.
- **Expiry notifications:** If 15 minutes pass without activity—or if a 60-second confirmation times out—the bot clears the active session and notifies you on XMPP to specify a recipient number for your next message.
- **Multi-sender protection:** If two different numbers text you within 15 minutes, sticky replies pause automatically and require you to specify the recipient's number.

**3. Built-in chat commands:**
- `status`: Check whether a sticky reply session is active and how many minutes remain.
- `help`: Display a quick formatting and command summary.

## Updating

To update the bridge inside the LXC container to the latest GitHub release (including `cloudflared` via `apt`), run:

```bash
update
```

## Testing

A unit test suite is available in the `/tests` directory. See [`tests/README.md`](tests/README.md) for instructions on running the tests.
