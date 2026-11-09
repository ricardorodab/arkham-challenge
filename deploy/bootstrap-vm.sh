#!/usr/bin/env bash
# Run once as root on Ubuntu 24.04, or repeat to verify the same host configuration.
set -Eeuo pipefail
umask 077
ulimit -c 0
[[ ${EUID} -eq 0 ]] || { echo 'Root is required.' >&2; exit 1; }
hostname=${1:?Usage: bootstrap-vm.sh public-dns-hostname}
[[ "$hostname" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$hostname" == *.* ]] || {
  echo 'An exact public DNS hostname is required.' >&2; exit 1;
}
for directory in /etc/arkham /opt/arkham /opt/arkham/releases /srv/arkham; do
  [[ ! -L "$directory" ]] || { echo 'Deployment directories cannot be symlinks.' >&2; exit 1; }
  install -d -m 0700 -o root -g root "$directory"
done
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl python3 docker.io docker-compose-v2 util-linux
fi
systemctl enable --now docker >/dev/null
for directory in /srv/arkham/data /srv/arkham/state; do
  [[ ! -L "$directory" ]] || { echo 'Persistent directories cannot be symlinks.' >&2; exit 1; }
  # Never recursively change an established workspace or replace its contents.
  install -d -m 0700 -o 10001 -g 10001 "$directory"
done
install -d -m 0700 /srv/arkham/caddy /srv/arkham/caddy/data /srv/arkham/caddy/config
python3 - "$hostname" <<'PY'
import json
import os
import sys
from pathlib import Path

root = Path('/etc/arkham')
files = {
    'deployment.json': {'public_host': sys.argv[1]},
    'key-vault.json': {'vault_url': 'https://arkhamkv.vault.azure.net', 'bootstrap_required': False},
}
for name, values in files.items():
    path = root / name
    if path.is_symlink():
        raise SystemExit('Secret files cannot be symlinks.')
    if path.exists():
        existing = json.loads(path.read_text())
        if name == 'deployment.json' and existing != values:
            raise SystemExit('The configured hostname differs; reconcile DNS explicitly.')
        if name == 'key-vault.json' and existing.get('vault_url') != values['vault_url']:
            raise SystemExit('The configured Key Vault differs; reconcile it explicitly.')
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(values, stream)
            stream.flush()
            os.fsync(stream.fileno())
    os.chown(path, 0, 0)
    os.chmod(path, 0o600)
PY
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install -d -m 0700 /opt/arkham/operations
for name in key-vault-secrets.py backup.py restore.py backup-run.sh restore-drill.sh app-control.py verify-state.py; do
  install -m 0700 "$source_dir/$name" "/opt/arkham/operations/$name"
done
chmod 0644 /opt/arkham/operations/app-control.py
install -d -m 0755 /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/arkham-secrets.conf <<'EOF'
[Unit]
Wants=network-online.target
After=network-online.target
[Service]
UMask=0077
LimitCORE=0
ExecStartPre=/usr/bin/python3 /opt/arkham/operations/key-vault-secrets.py fetch
EOF
systemctl daemon-reload
echo '{"bootstrap":"ready","secrets":"key_vault","data":"preserved"}'
