#!/usr/bin/env bash
_CS_DEFAULT_URL="https://raw.githubusercontent.com/aliicex/aa-sms-xmpp-bridge/main"
source <(curl -fsSL https://raw.githubusercontent.com/community-scripts/core/main/core/build.func)

APP="SMS-XMPP-Bridge"
var_tags="${var_tags:-messaging;xmpp;sms}"
var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-512}"
var_disk="${var_disk:-4}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_unprivileged="${var_unprivileged:-1}"

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if [[ ! -d /opt/sms-xmpp-bridge ]]; then
    msg_error "No ${APP} Installation Found!"
    exit 233
  fi

  if check_for_gh_release "sms-xmpp-bridge" "aliicex/aa-sms-xmpp-bridge"; then
    msg_info "Stopping Service"
    systemctl stop sms-xmpp-bridge
    msg_ok "Stopped Service"

    CLEAN_INSTALL=1 fetch_and_deploy_gh_release "sms-xmpp-bridge" "aliicex/aa-sms-xmpp-bridge" "tarball"

    msg_info "Updating Dependencies"
    cd /opt/sms-xmpp-bridge || exit
    $STD uv venv --clear .venv
    $STD uv pip install --python .venv/bin/python slixmpp aiohttp python-dotenv
    msg_ok "Updated Dependencies"

    msg_info "Starting Service"
    systemctl start sms-xmpp-bridge
    msg_ok "Started Service"
    msg_ok "Updated successfully!"
  fi
  exit
}

start
build_container
description

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW} Webhook endpoint is listening at:${CL}"
echo -e "${TAB}${GATEWAY}${BGN}http://${IP}:8080/webhook${CL}"
echo -e "${INFO}${YW} Populate /etc/sms-xmpp-bridge/secrets.env and run: systemctl restart sms-xmpp-bridge${CL}"
