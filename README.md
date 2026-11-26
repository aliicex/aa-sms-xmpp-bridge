# SMS to XMPP Bridge (Proxmox)

This project provides a Community Script for Proxmox VE that deploys a secure Debian LXC container. The container runs an asynchronous Python bot that acts as a bidirectional bridge between the Andrews & Arnold (AAISP) SMS API and an XMPP client (e.g., Cheogram).

## Features
- **Inbound SMS:** Received via AAISP HTTP Webhook -> Pushed to XMPP chat.
- **Outbound SMS:** Sent via XMPP message using the format `+number|message` -> AAISP HTTP POST.
- **Security:** Secrets are stored in a root-only environment file, injected just-in-time, and never logged.
- **Reliability:** Built with `asyncio` to handle concurrent incoming and outgoing messages.
- **Multi-part Support:** Handled natively by the AAISP API.

## Installation via Proxmox

To install this as a Community Script, run the following command on your Proxmox VE host:

`bash -c "$(curl -sL https://raw.githubusercontent.com/YOUR_REPO/ct/sms-xmpp-bridge.sh)"`

## Configuration & Secrets

The installation script creates a secure, root-only configuration file inside the container. The bot will not start successfully until this file is populated.

### 1. Populate the Secrets File
Log into your Proxmox container console or SSH into the container and edit the secrets file:

`nano /etc/sms-xmpp-bridge/secrets.env`

Fill in the values as follows (refer to `.env.example` in this repo for details):
- `XMPP_JID`: The JID of the bot account.
- `XMPP_PASSWORD`: The password for the bot account.
- `AA_USERNAME`: Your AAISP SMS username (usually your phone number).
- `AA_PASSWORD`: Your AAISP SMS API password.
- `WEBHOOK_TOKEN`: A random string of your choice (used to secure your inbound webhook).
- `TARGET_USER_JID`: Your personal XMPP JID where notifications should be sent.

### 2. Apply Changes
Once the file is saved, restart the service to apply the credentials:

`systemctl restart sms-xmpp-bridge`

### 3. Verify Status
You can check if the bot connected to XMPP and started the webhook server by checking the logs:

`journalctl -u sms-xmpp-bridge -f`

## Network & Port Configuration

### Ports
- **Port 8080 (TCP):** This port is used by the bot to listen for inbound SMS webhooks.

### External Access
Because the AAISP API needs to send HTTP POST requests to your bot, you must expose port 8080 to the internet. 

**Recommended Setup:**
Use a Reverse Proxy (like Nginx Proxy Manager) to map a public HTTPS URL to your container's internal IP:
`https://sms.yourdomain.com/webhook` -> `http://[CONTAINER_IP]:8080/webhook`

## Setup in AAISP Control Panel

To receive messages, log into your AAISP control panel and set the **Inbound SMS Target** for your number to:

`https://sms.yourdomain.com/webhook?token=YOUR_WEBHOOK_TOKEN`

## Usage

### Receiving SMS
When someone sends an SMS to your AAISP number, you will receive an XMPP message containing the sender's number and the message body.

### Sending SMS
To send an SMS from your XMPP client, send a message to the bot in this format:
`+447700900000|Hello! This is a test message from XMPP.`

## Testing
For developers, a full test suite is available in the `/tests` directory. See `tests/README.md` for instructions on running the tests.
