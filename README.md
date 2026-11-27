# SMS to XMPP Bridge for Proxmox

This project provides a community script for Proxmox VE. It deploys a lightweight Debian LXC container running an asynchronous Python bot (`slixmpp` and `aiohttp`) and `cloudflared`. Together, they provide a secure, bidirectional bridge between the Andrews & Arnold (AAISP) SMS API and an XMPP client such as Cheogram, Conversations or Gajim.

## Features

- **Incoming SMS:** Messages are received through a Cloudflare Tunnel, sent to a local webhook at `localhost:8080` and forwarded to your XMPP chat. No firewall ports need to be opened.
- **Outgoing SMS:** Send messages from XMPP using UK mobile numbers in `07...` or `+447...` format, or reply directly to an incoming message. The bot sends messages through an AAISP HTTP POST request, asks you to confirm with `y` or `n`, and reports any delivery errors in XMPP.
- **Spending controls:** The bot previews the number of SMS parts and estimated cost (about 3.6p per part), warns about Unicode and emoji encoding, converts supported smart punctuation to GSM-7, accepts only UK mobile numbers beginning with `+447`, limits messages to four parts and caps sending at 30 SMS per hour.
- **Security:** Secrets are stored in a root-only environment file. Webhook tokens are checked in constant time. Presence subscriptions are restricted to `TARGET_USER_JID`, and OMEMO-encrypted stanzas receive a plain-text warning.
- **Reliability:** The bot uses `asyncio` and handles `SIGTERM` gracefully, disconnecting cleanly when the container is updated.

## 1. Create a Cloudflare Tunnel

Because `cloudflared` runs inside the container, you do not need to open or forward any ports on your router.

1. Sign in to the [Cloudflare Zero Trust Dashboard](https://dash.cloudflare.com "Cloudflare Zero Trust Dashboard") and go to **Networks** > **Tunnels**.
2. Select **Create a tunnel**, choose **Cloudflared** and give the tunnel a name, such as `sms-xmpp-bridge`.
3. Copy the connector token shown on the installation page. You will enter it when you install the Proxmox script.
4. Open the tunnel's **Public Hostname** tab and add a route:
   - **Subdomain:** `sms`
   - **Domain:** `yourdomain.com`
   - **Path:** `webhook` (recommended, so that only `/webhook` is routed to the container)
   - **Service type:** `HTTP`
   - **URL:** `localhost:8080`

Do not apply a Cloudflare Access authentication policy to `https://sms.yourdomain.com/webhook`. AAISP's automated webhook requests cannot complete an interactive login.

## 2. Install on Proxmox

Run this command in the Proxmox VE host shell:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/aliicex/aa-sms-xmpp-bridge/main/ct/sms-xmpp-bridge.sh)"
```

During installation:

- A random `WEBHOOK_TOKEN` is generated and printed in the console.
- You will be asked for your Cloudflare Tunnel token. Press **Enter** to skip this step and run `cloudflared service install <TOKEN>` later.

## 3. Configure credentials

The installation script creates a root-only configuration file inside the container. The bot will not connect until you have added the required credentials.

### Edit the secrets file

Open the container console or connect over SSH, then edit `/etc/sms-xmpp-bridge/secrets.env`:

```bash
nano /etc/sms-xmpp-bridge/secrets.env
```

Enter the following values. See `.env.example` for the required format.

- `XMPP_JID`: The bot account's JID.
- `XMPP_PASSWORD`: The bot account's password.
- `AA_USERNAME`: Your AAISP VoIP number, for example `+44XXXXXXXXXX` or `0XXXXXXXXXX`.
- `AA_PASSWORD`: Your AAISP VoIP outgoing password. Set this in the AAISP Control Pages; it is not your main account password.
- `WEBHOOK_TOKEN`: This is populated during installation. Keep the generated value or replace it with a random string.
- `TARGET_USER_JID`: Your personal XMPP JID. Only this JID can send SMS, subscribe to the bot's presence and receive notifications.

### Restart the service and check its status

Restart the service to load your credentials, then follow the logs:

```bash
systemctl restart sms-xmpp-bridge
journalctl -u sms-xmpp-bridge -f
```

## 4. Configure the AAISP Control Pages

Sign in to the [AAISP Control Pages](https://control.aa.net.uk/ "Andrews & Arnold Control Pages") and open the VoIP settings for your number.

1. Set the **Inbound SMS Target** to:

   ```text
   https://sms.yourdomain.com/webhook?token=YOUR_WEBHOOK_TOKEN
   ```

   Replace `YOUR_WEBHOOK_TOKEN` with the value in `/etc/sms-xmpp-bridge/secrets.env`.

2. Set a monthly outbound call and SMS spending limit on the number. This provides an account-level backstop in addition to the bot's sending and message-length limits.

## Usage

### Receiving SMS

When an SMS arrives at your AAISP number, the bot forwards the sender's number and message to `TARGET_USER_JID`. It also reports whether direct replies are currently active.

SMS messages are not encrypted. Turn off OMEMO or PGP encryption for the bot chat in your XMPP client.

### Sending SMS to UK mobile numbers

UK mobile numbers in formats such as `07...`, `07700 900000`, `00447...` or `+447...` are converted to `+447...`. Non-UK numbers and non-mobile numbers are rejected.

**Start a new conversation**

Send a message beginning with the UK mobile number, followed by a space, colon, newline or pipe:

```text
07700900000 Hello! This is a test message.
07700 900000: Spaced numbers work with a colon or newline.
+447700900000 Hello!
```

**Reply to an active conversation**

After receiving an SMS, type your reply without a number within 15 minutes. For example:

```text
On my way!
```

Before sending, the bot shows the destination number, number of SMS parts, estimated cost (about 3.6p per part) and any Unicode or emoji warning. Reply `y` within 60 seconds to confirm, or `n` to cancel.

The bot clears the active session and notifies you if 15 minutes pass without activity or if a confirmation times out after 60 seconds. You will need to specify a recipient number for your next message.

If you receive messages from two different numbers within 15 minutes, direct replies are paused. Specify the recipient's number to send your next message.

**Commands**

- `status`: Check whether a direct-reply session is active and how many minutes remain.
- `help`: See a summary of message formats and commands.

## Updating

To update the bridge inside the LXC container to the latest GitHub release, including `cloudflared` through `apt`, run:

```bash
update
```

## Testing

A unit test suite is available in the `/tests` directory. See [`tests/README.md`](tests/README.md) for instructions on running it.
