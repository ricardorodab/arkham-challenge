#!/usr/bin/env bash
# Test a fresh VM snapshot with a SEPARATE temporary key, never the real recovery key.
set -Eeuo pipefail
umask 077
[[ $EUID == 0 ]] || exit 1
operations=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
keys=$(mktemp -d /run/arkham-drill.XXXXXXXX)
scratch="/srv/arkham-restore-tests/drill-$(cat /proc/sys/kernel/random/uuid)"
install -d -m 0700 "$scratch"
container=''
cleanup() {
  status=$?
  trap - EXIT
  if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null 2>&1 || true; fi
  for path in "$keys" "$scratch"; do
    checked=$(realpath -- "$path")
    case "$checked" in
      /run/arkham-drill.*|/srv/arkham-restore-tests/drill-*) rm -rf -- "$checked" ;;
      *) echo 'Refused unsafe drill cleanup path.' >&2; status=1 ;;
    esac
  done
  exit "$status"
}
trap cleanup EXIT
age-keygen -o "$keys/identity" 2>/dev/null
recipient=$(age-keygen -y "$keys/identity")
bash "$operations/backup-run.sh" --force --drill-recipient "$recipient" --receipt "$scratch/receipt.json"
blob=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["blob"])' "$scratch/receipt.json")
python3 "$operations/restore.py" download --blob "$blob" --archive "$scratch/backup.age"
python3 "$operations/restore.py" restore --archive "$scratch/backup.age" --identity "$keys/identity" \
  --base "$scratch" --target "$scratch/restored"
image=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["image"])' "$scratch/restored/manifest.json")
revision=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["revision"])' "$scratch/restored/manifest.json")
[[ "$image" =~ ^ghcr.io/ricardorodab/arkham-challenge-vm@sha256:[0-9a-f]{64}$ && "$revision" =~ ^[0-9a-f]{40}$ ]] || exit 1
[[ $(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image") == "$revision" ]] || exit 1
[[ $(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.source" }}' "$image") == https://github.com/ricardorodab/arkham-challenge ]] || exit 1
install -d -m 0700 "$scratch/restored/data"
chown -R 10001:10001 "$scratch/restored/state" "$scratch/restored/data"
container=$(docker run -d --network none --user 10001:10001 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 128 --memory 1536m \
  --tmpfs /tmp:rw,noexec,nosuid,size=128m,mode=1777 \
  --env-file "$scratch/restored/config/runtime.env" \
  -e OUTAGE_LOCAL_HTTP=1 -e OUTAGE_ALLOWED_HOSTS=localhost,127.0.0.1 \
  -e OUTAGE_DEPLOY_REVISION="$revision" -e OUTAGE_STATE_DIR=/appdata/state -e OUTAGE_DATA_DIR=/appdata/data \
  --mount "type=bind,src=$scratch/restored/state,dst=/appdata/state" \
  --mount "type=bind,src=$scratch/restored/data,dst=/appdata/data" \
  --mount "type=bind,src=$operations/app-control.py,dst=/deployment/app-control.py,readonly" \
  "$image" python -m outage_explorer.web.serve --host 0.0.0.0 --port 8000)
healthy=0
for ((attempt=0; attempt<20; attempt++)); do
  if docker exec "$container" python /deployment/app-control.py health >/dev/null 2>&1; then healthy=1; break; fi
  sleep 1
done
[[ $healthy == 1 ]] || { echo 'The isolated restored application failed health.' >&2; exit 1; }
docker exec "$container" python -c 'import os;os.environ.setdefault("DJANGO_SETTINGS_MODULE","outage_explorer.web.settings");import django;django.setup();from django.contrib.auth import get_user_model;print("Restored account database readable:",get_user_model().objects.count())'
docker rm -f "$container" >/dev/null
container=''
python3 "$operations/restore.py" verify --target "$scratch/restored"
echo '{"drill":"passed","production_state":"untouched","key":"separate_temporary_key","recovery_key_tested":false}'
