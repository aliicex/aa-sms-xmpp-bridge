#!/usr/bin/env bash
# SMS to XMPP Bridge Installation Script for Proxmox VE

set -euo pipefail

# Load the Community Scripts Core Engine
. <(curl -sSL https://raw.githubusercontent.com/community-scripts/core/main/core.sh)

# Application Metadata
app_name="SMS-XMPP-Bridge"
app_desc="Bridges Andrews & Arnold SMS API to XMPP (e.g., Cheogram)"
app_install_url="https://raw.githubusercontent.com/YOUR_REPO/install/sms-xmpp-bridge.sh"

# Default Resource Requirements
var_cpu=1
var_ram=512
var_disk=4
var_os="debian"
var_unprivileged=1
var_nesting=1

# Script logic
install_sms_xmpp_bridge() {
    # Creating the container using the core engine
    # The engine handles the wizard (Default vs Advanced)
    create_lxc_container "$app_name" "$app_install_url"
}

install_sms_xmpp_bridge
