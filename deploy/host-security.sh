#!/usr/bin/env bash
# Install before granting the VM identity any Blob data-plane permission.
set -Eeuo pipefail
umask 077
[[ $EUID == 0 ]] || exit 1
prepare_imds() {
  # DOCKER-USER applies to forwarded traffic from all Docker bridge networks.
  # Host-network/privileged containers require host-root authority and are forbidden here.
  iptables -w 10 -nL DOCKER-USER >/dev/null 2>&1 || iptables -w 10 -N DOCKER-USER
  iptables -w 10 -C DOCKER-USER -d 169.254.169.254/32 -m comment --comment arkham-deny-imds -j REJECT 2>/dev/null \
    || iptables -w 10 -I DOCKER-USER 1 -d 169.254.169.254/32 -m comment --comment arkham-deny-imds -j REJECT
  iptables -w 10 -C DOCKER-USER -d 168.63.129.16/32 -p tcp -m multiport --dports 80,32526 -m comment --comment arkham-deny-wireserver -j REJECT 2>/dev/null \
    || iptables -w 10 -I DOCKER-USER 1 -d 168.63.129.16/32 -p tcp -m multiport --dports 80,32526 -m comment --comment arkham-deny-wireserver -j REJECT
  iptables -w 10 -C DOCKER-USER -d 169.254.169.254/32 -m comment --comment arkham-deny-imds -j REJECT
  iptables -w 10 -C DOCKER-USER -d 168.63.129.16/32 -p tcp -m multiport --dports 80,32526 -m comment --comment arkham-deny-wireserver -j REJECT
}
block_imds() {
  prepare_imds
  # Fail closed when the daemon uses an unsupported backend/disconnected user chain.
  iptables -w 10 -C FORWARD -j DOCKER-USER
  iptables -w 10 -C DOCKER-USER -d 169.254.169.254/32 -m comment --comment arkham-deny-imds -j REJECT
  iptables -w 10 -C DOCKER-USER -d 168.63.129.16/32 -p tcp -m multiport --dports 80,32526 -m comment --comment arkham-deny-wireserver -j REJECT
}
case ${1:-} in
  prepare-imds) prepare_imds; exit 0 ;;
  block-imds) block_imds; exit 0 ;;
  install) ;;
  *) echo 'Usage: host-security.sh block-imds|install' >&2; exit 1 ;;
esac
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v age >/dev/null
command -v age-keygen >/dev/null
python3 "$source_dir/backup.py" --help >/dev/null
block_imds
install -d -m 0700 /opt/arkham/operations /var/lib/arkham-backup /srv/arkham-backup-staging /srv/arkham-restore-tests
for name in backup.py restore.py backup-run.sh restore-drill.sh host-security.sh app-control.py verify-state.py; do
  install -m 0700 "$source_dir/$name" "/opt/arkham/operations/$name"
done
# This public helper is bind-mounted into a UID10001 drill container. The parent
# directory remains root-only; the file itself must be readable inside that mount.
chmod 0644 /opt/arkham/operations/app-control.py
install -d -m 0755 /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/arkham-imds.conf <<'EOF'
[Service]
ExecStartPre=/bin/bash /opt/arkham/operations/host-security.sh prepare-imds
ExecStartPost=/bin/bash /opt/arkham/operations/host-security.sh block-imds
EOF
cat > /etc/systemd/system/arkham-host-security.service <<'EOF'
[Unit]
Description=Block Docker bridge access to Azure privileged metadata services
Requires=docker.service
After=docker.service
Before=arkham-backup.service
[Service]
Type=oneshot
ExecStart=/bin/bash /opt/arkham/operations/host-security.sh block-imds
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/arkham-backup.service <<'EOF'
[Unit]
Description=Create a consistent encrypted off-host Arkham backup
Requires=arkham-host-security.service
After=network-online.target docker.service arkham-host-security.service
ConditionPathExists=/etc/arkham/backup.json
[Service]
Type=oneshot
UMask=0077
ExecStart=/bin/bash /opt/arkham/operations/backup-run.sh
SuccessExitStatus=75
TimeoutStartSec=2h
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
EOF
cat > /etc/systemd/system/arkham-backup.timer <<'EOF'
[Unit]
Description=Check backup eligibility hourly and retry deferred/failed backups
[Timer]
OnCalendar=hourly
RandomizedDelaySec=15m
Persistent=true
[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/arkham-backup-check.service <<'EOF'
[Unit]
Description=Verify remote backup freshness and encrypted object receipt
After=network-online.target arkham-host-security.service
ConditionPathExists=/etc/arkham/backup.json
[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/bin/python3 /opt/arkham/operations/backup.py check --max-age 108000
EOF
cat > /etc/systemd/system/arkham-backup-check.timer <<'EOF'
[Unit]
Description=Check off-host backup freshness every thirty minutes
[Timer]
OnCalendar=*:0/30
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now arkham-host-security.service >/dev/null
systemctl enable --now arkham-backup.timer arkham-backup-check.timer >/dev/null
echo '{"host_security":"installed","container_imds":"blocked","container_wireserver":"blocked","timers":"enabled"}'
