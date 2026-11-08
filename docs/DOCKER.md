# Build and run with Docker Compose

Docker packages Python, Node and the locked application dependencies together. Use Docker Engine or Docker Desktop with **Linux containers** and the Docker Compose plugin. Run the commands from the repository root. The default deployment serves this computer at `http://127.0.0.1:8000`; it does not configure public hosting, HTTPS or a reverse proxy.

## Before startup: configure live data access

Building the image, initializing accounts, logging in and reading an existing publication do **not** require an EIA key. Live ingestion and Admin refresh do. For those operations, set your own `EIA_API_KEY` in the **same terminal before the first `docker compose up`** so the worker is created with it. The key is a runtime input, not a build argument.

PowerShell (private interactive input, without the literal key in command history):

```powershell
$env:EIA_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'EIA API key' -AsSecureString)).Password
```

Bash:

```bash
read -rsp 'EIA API key: ' EIA_API_KEY
printf '\n'
export EIA_API_KEY
```

Compose resolves the worker's `EIA_API_KEY` from the shell or a project `.env` used for interpolation. A shell value takes precedence, including an explicitly empty value. If you deliberately use `--env-file` instead, supply that option consistently when creating or recreating services. Only `worker` receives the resolved key; `app` and `init` do not. A one-off ingestion container receives it only when you explicitly pass `-e EIA_API_KEY`.

Neither `.env.example` nor `key.txt` is loaded automatically. The Python application does not load dotenv files, and this setup has no `_FILE` adapter or Docker secret-file mount for the key. Do not share expanded Compose configuration or put credentials in the image. If containers already exist, changing the shell or `.env` alone does not update their environment; use the [recovery steps](#already-started-without-the-key).

## Start a fresh workspace

```sh
docker compose up --build -d app
docker compose ps
```

The one-shot `init` service generates a private Django signing key and independent random passwords, runs migrations and creates `viewer`, `analyst` and `admin`. The app starts after initialization succeeds and its separate continuous refresh worker starts. The worker is a normal dependency, so the command above starts it without an optional profile. Repeated initialization applies migrations and preserves the signing key plus Admin-managed usernames, deletions, roles and passwords. Demo accounts are seeded only when no product personas exist or an operator explicitly requests `init --reset-passwords` recovery, which may require account-conflict reconciliation.

Open [the login page](http://127.0.0.1:8000/login/). To display the generated account passwords **only in your private terminal**, run this explicit operator command. It does not print the Django signing key:

```sh
docker compose exec -T app python -c "import json,os; from pathlib import Path; c=json.loads((Path(os.environ['OUTAGE_STATE_DIR'])/'local-config.json').read_text()); print('\n'.join(role+': '+c['OUTAGE_'+role.upper()+'_PASSWORD'] for role in ('viewer','analyst','admin')))"
```

Do not include that output in shared logs or screenshots. These are initialization credentials; later Admin password resets do not update this file. The application does not print passwords during normal startup. [The API contract](API_CONTRACT.md) defines the three personas and their permissions.

A new workspace has accounts but **no analytical publication**. Container startup does not enqueue an extraction. Login and process health can succeed before ingestion; analytical requests need data. The worker can consume previously queued jobs as soon as it starts. If a refresh is claimed without a key, it ends as `failed` with `refresh_not_configured` and configuration guidance; existing data remains unchanged.

## Load data explicitly

With the key still set in the current shell, run a bounded three-day extraction in a separate temporary container. If you cleared it after startup, [set it again](#before-startup-configure-live-data-access): this new CLI container does not borrow the running worker's environment.

```sh
docker compose run --rm --no-deps -e EIA_API_KEY app python -m outage_explorer ingest --mode full --start 2026-09-01 --end 2026-09-03 --data-dir /appdata/data
```

`-e EIA_API_KEY` forwards the already-set shell value without putting its literal value in the command. This command needs network access and consumes the provider's API quota; the sample dates are an example, not a claim of fresh live verification. It builds and publishes the matching analytical model before returning. The browser can adopt a newer model with **Use latest**.

After the command succeeds, inspect the matching retained publication and verify it without an EIA key:

```sh
docker compose run --rm --no-deps app python -m outage_explorer status --data-dir /appdata/data
docker compose run --rm --no-deps app python -m outage_explorer model status --data-dir /appdata/data
docker compose run --rm --no-deps app python -m outage_explorer model verify --data-dir /appdata/data
```

Status should identify a current snapshot and model, and verification should succeed. CLI ingestion creates a publication, not a refresh job; reload the workspace or choose **Use latest** when offered. For a refresh requested in the browser, wait for `succeeded`, then choose **Use latest**. A healthy container alone does not prove data has been published, and the Admin-only readiness endpoint requires both a model and the query runtime.

For a later correction-window refresh, replace the ingest options with `--mode recent --data-dir /appdata/data`. Omit `--start` and `--end` from full ingestion only when you intend to download all advertised history. These examples name `--data-dir` explicitly for clarity. The image also uses `/appdata` as its working directory, so the CLI default `data` resolves to the same persistent `/appdata/data`. The CLI itself does not read `OUTAGE_DATA_DIR`. [Local operations](LOCAL_OPERATIONS.md#3-load-or-refresh-source-data) covers extraction semantics and limits.

Remove the key from the shell when finished: `Remove-Item Env:EIA_API_KEY` in PowerShell, or `unset EIA_API_KEY` in Bash. The normal app service is not configured with an EIA key. Runtime environment values remain inspectable by Docker operators; do not bake credentials into an image, Dockerfile or build arguments.

## Configure the refresh worker

Admin refresh requests enqueue durable jobs. The default app startup command also starts the continuous worker that consumes them; ingestion still runs outside the HTTP process. To apply this configuration to an older workspace, rerun `docker compose up --build -d app`. Existing volumes and queued jobs are retained for the worker to process.

Before an upgrade, let active refreshes finish and make `EIA_API_KEY` available in the same terminal again (or through your configured `.env`). Recreated containers receive the current Compose environment; Compose does not copy the key from an older worker. New workers report download counts and source/model/publication stages in Refresh activity. Model export has its own percentage; total download work remains unknown. Older completed jobs keep their outcome without fabricated progress measurements.

The worker shares the app's data, application database and generated Django configuration. One worker owns the control lock; additional workers cannot consume concurrently. Configure its key [before startup](#before-startup-configure-live-data-access), or follow the existing-stack recovery below.

### Already started without the key

Starting the app first does not corrupt its state or require a volume reset. Login and existing data remain usable. If the worker was created without a key, it keeps that empty environment even after you configure the shell or `.env`. In that case, leave the app running and follow these steps. If a refresh is currently running, let it finish before deliberately recreating its worker.

1. Set `EIA_API_KEY` privately in the same terminal, using the [PowerShell or Bash prompt](#before-startup-configure-live-data-access).
2. For this already-initialized workspace, recreate **only** the worker; the app and named volumes stay in place:

```sh
docker compose up -d --no-deps --force-recreate worker
docker compose ps worker
docker compose logs --tail 30 worker
```

`--no-deps` is appropriate here because initialization has already completed; fresh workspaces must use the normal startup command instead. [Recreation preserves mounted volumes](https://docs.docker.com/reference/cli/docker/compose/up/); changing only the key does not require rebuilding the image. [A simple `docker compose restart worker` does not apply environment changes](https://docs.docker.com/reference/cli/docker/compose/restart/).

3. Watch the existing job in Admin refresh activity. A still-`queued` job can be claimed automatically, so do not submit a duplicate while it is queued or running. If it already failed with `refresh_not_configured`, **queue a new refresh** after recreation. Completed failed jobs are not reset or automatically requeued; replaying an old idempotency key still returns the original failed outcome. On success, choose **Use latest**.

You may clear the shell key after recreation; the running worker retains its process environment. Supply it again before a later worker recreation. If a job remains queued, inspect `docker compose ps worker` and `docker compose logs worker`. Starting the worker allows it to claim existing queued jobs.

To pause refresh execution while leaving the app running:

```sh
docker compose stop worker
```

A later `docker compose up -d app` starts that dependency again. An interrupted job may already have published data because catalog publication and Django job completion are separate transactions. Inspect publication history before retrying. [Refresh operations](LOCAL_OPERATIONS.md#4-execute-admin-refresh-jobs) explains recovery and idempotency.

## Ports, paths and persisted state

The app listens on `0.0.0.0:8000` **inside** its container. Compose publishes it only on host loopback. Set `OUTAGE_PORT` before startup to choose another host port: `$env:OUTAGE_PORT = '8080'` in PowerShell or `export OUTAGE_PORT=8080` in Bash, then run `docker compose up -d app`. Open `http://127.0.0.1:8080`. This changes the published port, not allowed hosts or the internal listener.

`OUTAGE_IMAGE` selects the image tag (default `arkham:local`) when using an image you have built or loaded separately. Compose may read a project `.env` for variable interpolation, including the port, image tag and worker key. The Python application itself does not load `.env`; no environment file is required or generated by this workflow. Prefer the shell prompt above for the EIA key, and do not share expanded Compose configuration containing it.

| Container path | Contents and lifetime |
| --- | --- |
| `/opt/outage` | Trusted image files and installed npm runtime; no generated operator data |
| `/appdata/data` | Named `data` volume: catalog database, immutable raw/model bundles, manifests and unfinished candidates |
| `/appdata/state` | Named `state` volume: private `local-config.json`, users, sessions, jobs, query cache and admission/worker locks |
| `/tmp` | 128 MiB temporary in-memory scratch space; not persistent storage |

The image runs as UID/GID **10001**, with pre-owned volume directories, a read-only root filesystem, dropped Linux capabilities and no new privileges. The app and worker each have a 2 GiB container memory cap. Both data and state volumes must be writable: even analytical catalog opening can initialize SQLite/schema state. The SQL compiler and engine retain their separate child-process controls; a container does not replace the [query isolation boundary](QUERY_SECURITY.md).

The image contains installed Python package assets and the locked server-side npm runtime. Browser assets are served by Django's allowlisted packaged views; no frontend build or separate static server is needed. Builds exclude local datasets, generated credentials, private preparation and host dependency directories.

```sh
docker compose down
```

This stops/removes service containers and their network while preserving named volumes. Starting again reuses accounts and data. **`docker compose down -v` deletes the volumes, including data, passwords, signing key and sessions.** Use it only for an intentional reset. Container logs can be viewed with `docker compose logs app` or `docker compose logs worker`.

## Move or back up a workspace

Rebuilding the image on another machine creates the same application, not a copy of this machine's publication or accounts. For a new independent workspace, start fresh volumes and ingest with your own key.

To transfer an existing workspace, finish any one-off ingestion/model build and stop both app and worker. Preserve the **complete data and state volumes together**, including SQLite WAL files and every referenced immutable bundle. Follow [Docker's volume backup/restore procedure](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes), restore into empty volumes, and preserve writable ownership for UID/GID 10001. Do not merge a backup over a running database. A host checkout's existing `data/` and `.service/` are not mounted or imported automatically; importing them requires the same quiesced full-directory transfer and ownership checks.

Treat state backups as private: they contain the Django key, generated seed passwords, password hashes and sessions. Test the restored copy before retiring the original. Named volumes survive container replacement but are not off-machine backups, and this workflow does not establish a universal disk/power-loss or atomic cross-database restore guarantee. [Recovery and backup limits](LOCAL_OPERATIONS.md#8-recovery-backup-and-storage-limits) remain applicable.

## Health and verification

The image also built for `linux/arm64`, with dependency imports, runtime discovery and nonroot/read-only checks passing under Docker Desktop emulation. Full HTTP/WASM acceptance was verified on `linux/amd64`; native ARM query performance has not been measured.

The container healthcheck calls unauthenticated `/api/v1/health/live`. It proves that the HTTP process responds; it does not require a published dataset or establish query capacity. `/api/v1/health/ready` requires an authenticated Admin and checks model/runtime availability, not a complete query.

Local **Linux/amd64 Docker Desktop** acceptance passed the real production build and isolated Compose workflow: automatic/repeated initialization, nonroot runtime and asset checks, host HTTP/CSRF and all three personas, exact date/Decimal results through actual DuckDB-WASM, separate-worker refresh success/failure, bounded shutdown, session persistence and forced container replacement preserving credentials, models and job history. The separate build-context audit preserved all 75 required public files byte-for-byte while excluding 11 synthetic private-path canaries; its marker scan and owned-resource cleanup passed.

These optional host-side checks need Python 3.12+ and Docker, with no installed application dependencies:

```sh
docker build --tag arkham:local .
python tests/docker/context_privacy.py --image arkham:local
python tests/docker/acceptance.py --image arkham:local
```

Both scripts accept `--docker PATH` when the CLI is outside PATH. They create isolated resources and remove only resources they own. The [Docker acceptance instructions](../tests/docker/README.md) describe fixture and privacy scope. A container CI job runs the same build and two checks; [GitHub Actions](https://github.com/ricardorodab/arkham-challenge/actions) records completed remote results.

This acceptance uses synthetic upstream responses and does not establish a fresh live EIA extraction, public HTTPS operation or native ARM execution. Source CI and [historical verification](NOTES.md) retain their separate scope.
