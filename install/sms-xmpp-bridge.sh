#!/usr/bin/env bash
# Installation script running inside the LXC container

set -euo pipefail

# Update and install dependencies
apt-get update
apt-get install -y python3-pip python3-venv git curl ufw

# Create a dedicated system user for the bot
useradd -m -s /bin/bash smsbridge

# Set up application directory
mkdir -p /opt/sms-xmpp-bridge
chown smsbridge:smsbridge /opt/sms-xmpp-bridge

# Install Python environment
cd /opt/sms-xmpp-bridge
sudo -u smsbridge python3 -m venv venv
sudo -u smsbridge ./venv/bin/pip install slixmpp aiohttp python-dotenv

# Download the bot code (Assuming it's in your repo)
sudo -u smsbridge curl -sSL https://raw.githubusercontent.com/YOUR_REPO/bot/main/bot.py -o bot.py

# Configure Firewall (Expose only the inbound SMS webhook port)
ufw allow 8080/tcp
ufw --force enable

# Create Systemd service
cat <<EOF > /etc/systemd/system/sms-xmpp-bridge.service
[Unit]
Description=SMS to XMPP Bridge Bot
After=network.target

[Service]
User=smsbridge
WorkingDirectory=/opt/sms-xmpp-bridge
EnvironmentFile=/etc/sms-xmpp-bridge/secrets.env
ExecStart=/opt/sms-xmpp-bridge/venv/bin/python3 /opt/sms-xmpp-bridge/bot.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# Create secure directory for secrets
mkdir -p /etc/sms-xmpp-bridge
chmod 700 /etc/sms-xmpp-bridge

# Generate a secure random token for the webhook
# This creates a 32-character hex string
WEBHOOK_TOKEN=$(openssl rand -hex 16)

# Pre-populate the secrets file with the token and placeholders
cat <<EOF > /etc/sms-xmpp-bridge/secrets.env
XMPP_JID=
XMPP_PASSWORD=
AA_USERNAME=
AA_PASSWORD=
WEBHOOK_TOKEN=$WEBHOOK_TOKEN
TARGET_USER_JID=
EOF

chmod 600 /etc/sms-xmpp-bridge/secrets.env
chown -R root:root /etc/sms-xmpp-bridge

# Output the token to the console so the user can copy it for the AAISP panel
echo "----------------------------------------------------------------"
echo " INSTALLATION COMPLETE"
echo " Your generated Webhook Token is: $WEBHOOK_TOKEN"
echo " Copy this token and add it to your AAISP Inbound Target URL."
echo "----------------------------------------------------------------"


systemctl daemon-reload
systemctl enable sms-xmpp-bridge.service
