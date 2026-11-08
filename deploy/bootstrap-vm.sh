#!/usr/bin/env bash
# Run once as root on Ubuntu 24.04, or repeat to verify the same host configuration.
set -Eeuo pipefail
umask 077
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
if [[ -e /etc/arkham/deployment.env ]]; then
  expected="OUTAGE_PUBLIC_HOST=$hostname"
  [[ $(cat /etc/arkham/deployment.env) == "$expected" ]] || {
    echo 'The configured hostname differs; reconcile DNS explicitly before continuing.' >&2; exit 1;
  }
else
  printf 'OUTAGE_PUBLIC_HOST=%s\n' "$hostname" > /etc/arkham/deployment.env
fi
python3 - <<'PY'
import os
import secrets
from pathlib import Path

root = Path('/etc/arkham')
files = {
    'runtime.env': {'OUTAGE_DJANGO_SECRET_KEY': secrets.token_urlsafe(64)},
    'bootstrap.env': {
        f'OUTAGE_{role.upper()}_PASSWORD': secrets.token_urlsafe(24)
        for role in ('viewer', 'analyst', 'admin')
    },
    'worker.env': {'EIA_API_KEY': ''},
}
for name, values in files.items():
    path = root / name
    if path.is_symlink():
        raise SystemExit('Secret files cannot be symlinks.')
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            for key, value in values.items():
                stream.write(f'{key}={value}\n')
            stream.flush()
            os.fsync(stream.fileno())
    os.chown(path, 0, 0)
    os.chmod(path, 0o600)
PY
chmod 0600 /etc/arkham/deployment.env
echo '{"bootstrap":"ready","secrets":"preserved_or_created","data":"preserved"}'
