#!/usr/bin/env bash
set -euo pipefail

KEEP_USER="${1:-ccttww}"
SSH_SOURCE_CIDR="${SSH_SOURCE_CIDR:-}"
BACKUP_ROOT="/root/bastion-hardening-backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${BACKUP_ROOT}/${STAMP}"

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "run as root: sudo bash $0 [user]" >&2
    exit 1
  fi
}

backup_file() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    local rel="${path#/}"
    install -d -m 0700 "${BACKUP_DIR}/$(dirname "${rel}")"
    cp -a "${path}" "${BACKUP_DIR}/${rel}"
  fi
}

write_sshd_dropin() {
  install -d -m 0755 /etc/ssh/sshd_config.d
  cat >/etc/ssh/sshd_config.d/90-bastion.conf <<EOF
# Managed by raspi-bastion-hardening.sh
Port 22
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PubkeyAuthentication yes
AuthenticationMethods publickey
AllowUsers ${KEEP_USER}
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding yes
GatewayPorts no
PermitTunnel no
PermitEmptyPasswords no
ClientAliveInterval 300
ClientAliveCountMax 2
LoginGraceTime 20
MaxAuthTries 3
MaxSessions 10
LogLevel VERBOSE
EOF
  sshd -t
}

write_fail2ban_jail() {
  install -d -m 0755 /etc/fail2ban/jail.d
  cat >/etc/fail2ban/jail.d/sshd-bastion.local <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 4
findtime = 10m
bantime = 1h
mode = aggressive
EOF
}

configure_ufw() {
  if ! command -v ufw >/dev/null 2>&1; then
    return 0
  fi

  ufw --force reset
  ufw default deny incoming
  ufw default allow outgoing
  if [[ -n "${SSH_SOURCE_CIDR}" ]]; then
    ufw allow proto tcp from "${SSH_SOURCE_CIDR}" to any port 22 comment 'OpenSSH'
  else
    ufw allow 22/tcp comment 'OpenSSH'
  fi
  ufw logging on
  ufw --force enable
}

disable_service_if_present() {
  local unit="$1"
  if systemctl list-unit-files --type=service --no-pager --no-legend | awk '{print $1}' | grep -Fxq "${unit}"; then
    systemctl disable --now "${unit}" || true
  fi
}

main() {
  require_root

  install -d -m 0700 "${BACKUP_DIR}"
  systemctl list-unit-files --type=service --no-pager >"${BACKUP_DIR}/service-units-before.txt" || true
  ss -lntup >"${BACKUP_DIR}/listeners-before.txt" || true
  ufw status verbose >"${BACKUP_DIR}/ufw-before.txt" 2>&1 || true

  backup_file /etc/ssh/sshd_config
  backup_file /etc/ssh/sshd_config.d
  backup_file /etc/fail2ban/jail.conf
  backup_file /etc/fail2ban/jail.d
  backup_file /etc/default/ufw
  backup_file /etc/ufw

  write_sshd_dropin
  write_fail2ban_jail
  configure_ufw

  for svc in \
    nginx.service \
    mysql.service \
    vsftpd.service \
    redis-server.service \
    cups.service \
    cups-browsed.service \
    avahi-daemon.service \
    bluetooth.service \
    hciuart.service \
    ModemManager.service \
    packagekit.service \
    udisks2.service \
    upower.service \
    kerneloops.service
  do
    disable_service_if_present "${svc}"
  done

  if systemctl list-unit-files --type=service --no-pager --no-legend | awk '{print $1}' | grep -Fxq fail2ban.service; then
    systemctl enable --now fail2ban.service
    systemctl restart fail2ban.service
  fi

  systemctl restart ssh.service

  echo "bastion hardening complete"
  echo "backup: ${BACKUP_DIR}"
  systemctl --no-pager --plain --state=running --type=service
  echo
  ss -lntup
  echo
  ufw status verbose || true
}

main "$@"
