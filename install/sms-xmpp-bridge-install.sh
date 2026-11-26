#!/usr/bin/env bash

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

msg_info "Installing Cloudflared"
CF_PKG_HOST="pkg.cloudflare.com"
mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL "https://${CF_PKG_HOST}/cloudflare-main.gpg" | tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://${CF_PKG_HOST}/cloudflared any main" >/etc/apt/sources.list.d/cloudflared.list
$STD apt-get update
$STD apt-get install -y cloudflared
msg_ok "Installed Cloudflared"

fetch_and_deploy_gh_release "sms-xmpp-bridge" "aliicex/aa-sms-xmpp-bridge" "tarball"

PYTHON_VERSION="3.12" setup_uv

msg_info "Setting up Python Environment"
cd /opt/sms-xmpp-bridge
$STD uv venv .venv
$STD uv pip install --python .venv/bin/python slixmpp aiohttp python-dotenv
msg_ok "Set up Python Environment"

msg_info "Configuring Secrets"
mkdir -p /etc/sms-xmpp-bridge
chmod 700 /etc/sms-xmpp-bridge
WEBHOOK_TOKEN=$(random_password 32)
cat <<EOF >/etc/sms-xmpp-bridge/secrets.env
XMPP_JID=
XMPP_PASSWORD=
AA_USERNAME=
AA_PASSWORD=
WEBHOOK_TOKEN=$WEBHOOK_TOKEN
TARGET_USER_JID=
EOF
chmod 600 /etc/sms-xmpp-bridge/secrets.env
msg_ok "Configured Secrets (Webhook Token: $WEBHOOK_TOKEN)"

msg_info "Creating Service"
cat <<EOF >/etc/systemd/system/sms-xmpp-bridge.service
[Unit]
Description=SMS to XMPP Bridge Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/sms-xmpp-bridge
EnvironmentFile=/etc/sms-xmpp-bridge/secrets.env
ExecStart=/opt/sms-xmpp-bridge/.venv/bin/python /opt/sms-xmpp-bridge/src/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl enable -q --now sms-xmpp-bridge
msg_ok "Created Service"

CF_TOKEN=""
if [[ -r /dev/tty ]]; then
  echo ""
  read -r -p "${TAB3}Enter your Cloudflare Tunnel token (or press Enter to skip): " CF_TOKEN </dev/tty || true
fi

if [[ -n "$CF_TOKEN" ]]; then
  msg_info "Configuring Cloudflare Tunnel"
  $STD cloudflared service install "$CF_TOKEN"
  msg_ok "Configured Cloudflare Tunnel"
else
  echo -e "${INFO}${YW} Skipped Cloudflare Tunnel setup. To configure later, run:${CL}"
  echo -e "${TAB}${BGN}cloudflared service install <YOUR_TUNNEL_TOKEN>${CL}"
fi

motd_ssh
customize
cleanup_lxc
