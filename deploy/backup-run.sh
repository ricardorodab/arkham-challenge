#!/usr/bin/env bash
# Hourly eligibility/retry wrapper; a successful backup is due again after 20 hours.
set -Eeuo pipefail
umask 077
[[ $EUID == 0 ]] || exit 1
operations=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
force=0
recipient=''
receipt=/var/lib/arkham-backup/last-success.json
while (($#)); do
  case "$1" in
    --force) force=1; shift ;;
    --drill-recipient) recipient=${2:?}; shift 2 ;;
    --receipt) receipt=${2:?}; shift 2 ;;
    *) echo 'Unknown backup wrapper argument.' >&2; exit 1 ;;
  esac
done
if [[ $force == 0 ]] && python3 "$operations/backup.py" check --max-age 72000 >/dev/null 2>&1; then
  exit 0
fi
exec 9>/run/lock/arkham-deploy.lock
flock -n 9 || exit 75
release=$(readlink -f /opt/arkham/current)
[[ "$release" == /opt/arkham/releases/* && -f "$release/release.json" ]] || exit 1
OUTAGE_PUBLIC_HOST=$(python3 -c 'import json,sys;v=json.load(open(sys.argv[1]));assert set(v)=={"public_host"};print(v["public_host"])' /etc/arkham/deployment.json)
OUTAGE_IMAGE=$(python3 -c 'import json,sys;v=json.load(open(sys.argv[1]));assert set(v)=={"image"};print(v["image"])' "$release/release.json")
[[ "$OUTAGE_PUBLIC_HOST" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$OUTAGE_PUBLIC_HOST" == *.* ]] || exit 1
[[ "$OUTAGE_IMAGE" =~ ^ghcr.io/ricardorodab/arkham-challenge-vm@sha256:[0-9a-f]{64}$ ]] || exit 1
export OUTAGE_PUBLIC_HOST OUTAGE_IMAGE OUTAGE_RELEASE_DIR="$release"
export OUTAGE_DEPLOY_REVISION="$(basename "$release")"
export OUTAGE_SECRET_ROOT=/run/arkham-secrets/current
compose=(docker compose --project-name arkham --env-file /dev/null -f "$release/deploy/compose.production.yaml")
install -d -m 0700 /srv/arkham-backup-staging /var/lib/arkham-backup
stage="/srv/arkham-backup-staging/stage-$(cat /proc/sys/kernel/random/uuid)"
paused=0
worker_stopped=0
locks=0
cleanup() {
  status=$?
  trap - EXIT
  if [[ $locks == 1 ]]; then flock -u 7; flock -u 8; fi
  if [[ $paused == 1 ]]; then
    "${compose[@]}" up -d --no-deps --wait --wait-timeout 100 app >/dev/null 2>&1 || status=1
    if [[ $worker_stopped == 1 ]]; then
      "${compose[@]}" up -d --no-deps worker >/dev/null 2>&1 || status=1
    fi
  fi
  if [[ -d "$stage" ]]; then
    checked=$(realpath -- "$stage")
    [[ "$checked" == /srv/arkham-backup-staging/stage-* && "$checked" != /srv/arkham-backup-staging ]] || exit 1
    rm -rf -- "$checked"
  fi
  exit "$status"
}
trap cleanup EXIT
"${compose[@]}" stop -t 35 app >/dev/null
paused=1
if ! python3 "$operations/verify-state.py" idle-database /srv/arkham/state/web.sqlite3; then
  echo '{"backup":"deferred","reason":"active_refresh"}'
  exit 75
fi
"${compose[@]}" stop -t 30 worker >/dev/null
worker_stopped=1
exec 7<>/srv/arkham/data/.writer.lock
exec 8<>/srv/arkham/state/refresh-worker-control/.writer.lock
flock -n 7 && flock -n 8 || exit 75
locks=1
python3 "$operations/backup.py" capture --stage "$stage"
flock -u 7; flock -u 8
locks=0
"${compose[@]}" up -d --no-deps --wait --wait-timeout 100 app >/dev/null
"${compose[@]}" up -d --no-deps worker >/dev/null
worker_stopped=0
paused=0
# Release deployment serialization during the potentially long encrypted upload.
flock -u 9
arguments=(upload --stage "$stage" --receipt "$receipt")
if [[ -n "$recipient" ]]; then arguments+=(--drill-recipient "$recipient"); fi
python3 "$operations/backup.py" "${arguments[@]}"
