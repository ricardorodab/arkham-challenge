#!/usr/bin/env bash
# Apply a previously extracted immutable source release and a matching image digest.
set -Eeuo pipefail
umask 077
[[ ${EUID} -eq 0 ]] || { echo 'Root is required.' >&2; exit 1; }
revision=${1:?Usage: release.sh revision immutable-image-reference [docker-save-archive]}
image=${2:?An immutable image reference is required}
archive=${3:-}
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { echo 'A full commit SHA is required.' >&2; exit 1; }
[[ "$image" =~ @sha256:[0-9a-f]{64}$ ]] || {
  echo 'An image reference pinned by SHA256 digest is required.' >&2; exit 1;
}
release_dir="/opt/arkham/releases/$revision"
[[ -d "$release_dir/deploy" && ! -L "$release_dir" ]] || {
  echo 'The matching source release must already be extracted.' >&2; exit 1;
}
exec 9>/run/lock/arkham-deploy.lock
flock -n 9 || { echo '{"deployment":"busy"}'; exit 75; }
[[ -f /etc/arkham/deployment.env ]] || { echo 'Run bootstrap-vm.sh first.' >&2; exit 1; }
# This is a root-owned file containing only the validated public hostname.
source /etc/arkham/deployment.env
export OUTAGE_PUBLIC_HOST OUTAGE_IMAGE="$image" OUTAGE_DEPLOY_REVISION="$revision"
export OUTAGE_RELEASE_DIR="$release_dir"
[[ "$OUTAGE_PUBLIC_HOST" =~ ^[a-z0-9][a-z0-9.-]+[a-z0-9]$ ]] || exit 1
compose=(docker compose --project-name arkham --env-file /dev/null -f "$release_dir/deploy/compose.production.yaml")
old_dir=''
old_image=''
old_revision=''
if [[ -L /opt/arkham/current ]]; then
  old_dir=$(readlink -f /opt/arkham/current)
  [[ "$old_dir" == /opt/arkham/releases/* && -f "$old_dir/release.env" ]] || {
    echo 'The current release reference is invalid.' >&2; exit 1;
  }
  old_image=$(sed -n 's/^OUTAGE_IMAGE=//p' "$old_dir/release.env")
  old_revision=$(basename "$old_dir")
elif [[ -e /opt/arkham/current ]]; then
  echo 'The current release reference must be a symlink.' >&2; exit 1
fi
old_compose() {
  OUTAGE_RELEASE_DIR="$old_dir" OUTAGE_IMAGE="$old_image" OUTAGE_DEPLOY_REVISION="$old_revision" \
    docker compose --project-name arkham --env-file /dev/null -f "$old_dir/deploy/compose.production.yaml" "$@"
}
restore_previous() {
  old_compose up -d --no-deps --wait --wait-timeout 100 app >/dev/null 2>&1 \
    && old_compose up -d --no-deps worker proxy >/dev/null 2>&1
}
result_dir="$release_dir/result-$(date -u +%Y%m%dT%H%M%S)-$$"
install -d -m 0700 "$result_dir"
paused=0
changed=0
committed=0
release_locks() {
  [[ ${locks_held:-0} == 0 ]] || { flock -u 7; flock -u 8; locks_held=0; }
}
on_exit() {
  status=$?
  trap - EXIT
  release_locks
  unset GHCR_TOKEN
  if [[ -n ${registry_config:-} && -d "$registry_config" ]]; then
    rm -f -- "$registry_config/config.json"
    rmdir -- "$registry_config" 2>/dev/null || true
  fi
  if [[ $status -ne 0 && $paused == 1 && -n "$old_image" && $committed == 0 ]]; then
    if [[ $changed == 0 ]]; then
      restore_previous || echo '{"rollback":"previous_image_health_failed","state":"preserved"}'
    else
      # As in the forward upgrade, drain HTTP before examining the queue. Otherwise
      # an enqueue between the idle check and worker stop could interrupt ingestion.
      if ! "${compose[@]}" stop -t 35 app >/dev/null 2>&1; then
        echo '{"deployment":"failed","rollback":"app_drain_failed","state":"preserved"}'
      elif ! python3 "$release_dir/deploy/verify-state.py" idle-database /srv/arkham/state/web.sqlite3; then
        "${compose[@]}" up -d --no-deps --wait --wait-timeout 100 app >/dev/null 2>&1 || true
        echo '{"deployment":"failed","rollback":"deferred_for_active_refresh","state":"preserved"}'
      elif python3 "$release_dir/deploy/verify-state.py" compatible \
          "$result_dir/before.json" /srv/arkham/state/web.sqlite3; then
        "${compose[@]}" stop -t 30 worker >/dev/null 2>&1 || true
        if restore_previous; then
          echo '{"deployment":"failed","rollback":"previous_image_healthy","state":"preserved"}'
        else
          echo '{"deployment":"failed","rollback":"previous_image_health_failed","state":"preserved"}'
        fi
      else
        echo '{"deployment":"failed","rollback":"requires_operator_schema_review","state":"preserved"}'
      fi
    fi
  fi
  exit "$status"
}
trap on_exit EXIT

# Registry credentials exist only while pulling; Compose never receives the token.
if [[ -n "$archive" ]]; then
  docker load --input "$archive" >/dev/null
elif [[ -n ${GHCR_TOKEN:-} ]]; then
  registry_config=$(mktemp -d /run/arkham-registry.XXXXXXXX)
  chmod 0700 "$registry_config"
  printf '%s' "$GHCR_TOKEN" | docker --config "$registry_config" login ghcr.io \
    --username "${GHCR_USERNAME:?A registry user is required with GHCR_TOKEN}" --password-stdin >/dev/null 2>&1
  docker --config "$registry_config" pull "$image" >/dev/null
  rm -f -- "$registry_config/config.json"
  rmdir -- "$registry_config"
  registry_config=''
else
  docker pull "$image" >/dev/null
fi
unset GHCR_TOKEN GHCR_USERNAME
image_revision=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")
[[ "$image_revision" == "$revision" ]] || { echo 'Image revision does not match source release.' >&2; exit 1; }
# Fetch the pinned public proxy before pausing traffic.
"${compose[@]}" pull proxy >/dev/null
"${compose[@]}" config --quiet
printf 'OUTAGE_IMAGE=%s\n' "$image" > "$release_dir/release.env"

if [[ -n "$old_image" ]]; then
  # Close enqueueing before looking at the durable queue. Leave an active worker alone.
  old_compose stop -t 35 app >/dev/null
  paused=1
else
  # A failed first deployment can leave a provisional app without a current reference.
  "${compose[@]}" stop -t 35 app >/dev/null
fi
"${compose[@]}" run --rm --no-deps -T app python /deployment/app-control.py inspect > "$result_dir/before.json"
if ! python3 "$release_dir/deploy/verify-state.py" idle "$result_dir/before.json"; then
  echo '{"deployment":"deferred","reason":"active_refresh","state":"preserved"}'
  exit 75
fi
if [[ -n "$old_image" ]]; then
  old_compose stop -t 30 worker >/dev/null
else
  "${compose[@]}" stop -t 30 worker >/dev/null
fi
# Also exclude direct operator ingestion and any separately launched worker.
install -d -m 0700 -o 10001 -g 10001 /srv/arkham/state/refresh-worker-control
for lock in /srv/arkham/data/.writer.lock /srv/arkham/state/refresh-worker-control/.writer.lock; do
  if [[ ! -e "$lock" ]]; then
    install -m 0600 -o 10001 -g 10001 /dev/null "$lock"
  fi
done
exec 7<>/srv/arkham/data/.writer.lock
exec 8<>/srv/arkham/state/refresh-worker-control/.writer.lock
flock -n 7 && flock -n 8 || { echo '{"deployment":"deferred","reason":"operator_writer_active"}'; exit 75; }
locks_held=1
changed=1
# The initializer performs no extraction and never reseeds an established database.
"${compose[@]}" run --rm --no-deps -T init > "$result_dir/init.json"
"${compose[@]}" run --rm --no-deps -T app python /deployment/app-control.py inspect > "$result_dir/after.json"
python3 "$release_dir/deploy/verify-state.py" preserved "$result_dir/before.json" "$result_dir/after.json"
"${compose[@]}" up -d --no-deps --wait --wait-timeout 100 app >/dev/null
"${compose[@]}" up -d --no-deps proxy >/dev/null
# Verify the public origin, real TLS and exact application revision before promotion.
healthy=0
for ((attempt=0; attempt<36; attempt++)); do
  if curl --silent --show-error --fail --connect-timeout 5 --max-time 10 \
      --dump-header "$result_dir/https.headers" "https://$OUTAGE_PUBLIC_HOST/api/v1/health/live" \
      --output "$result_dir/https.json" 2>/dev/null \
      && python3 "$release_dir/deploy/verify-state.py" https \
        "$result_dir/https.headers" "$result_dir/https.json" "$revision"; then
    healthy=1
    break
  fi
  sleep 5
done
[[ $healthy == 1 ]] || { echo 'Public HTTPS revision health failed.' >&2; exit 1; }
release_locks
"${compose[@]}" up -d --no-deps worker >/dev/null
worker_id=$("${compose[@]}" ps -q worker)
sleep 2
[[ $(docker inspect --format '{{.State.Running}}:{{.RestartCount}}' "$worker_id") == true:0 ]] || {
  echo 'Refresh worker did not remain running.' >&2; exit 1;
}
# Atomic reference promotion happens only after app, TLS and worker checks pass.
ln -s "$release_dir" "/opt/arkham/.current.$$.next"
if [[ -n "$old_dir" && "$old_dir" != "$release_dir" ]]; then
  ln -s "$old_dir" "/opt/arkham/.previous.$$.next"
  mv -Tf "/opt/arkham/.previous.$$.next" /opt/arkham/previous
fi
mv -Tf "/opt/arkham/.current.$$.next" /opt/arkham/current
committed=1
echo "{\"deployment\":\"healthy\",\"revision\":\"$revision\",\"https\":true,\"state\":\"preserved\"}"
