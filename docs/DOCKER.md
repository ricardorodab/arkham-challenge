# Build and run with Docker Compose

Docker packages Python, Node and the locked application dependencies together. Use Docker Engine or Docker Desktop with **Linux containers** and the Docker Compose plugin. Run the commands from the repository root. The default deployment serves this computer at `http://127.0.0.1:8000`; it does not configure public hosting, HTTPS or a reverse proxy.

## Start a fresh workspace

```sh
docker compose up --build -d app
docker compose ps
```

The one-shot `init` service generates a private Django signing key and independent random passwords, runs migrations and creates `viewer`, `analyst` and `admin`. The app starts after initialization succeeds. Repeated initialization preserves the signing key and account passwords; password reset is an explicit operator action.

Open [the login page](http://127.0.0.1:8000/login/). To display the generated account passwords **only in your private terminal**, run this explicit operator command. It does not print the Django signing key:

```sh
docker compose exec -T app python -c "import json,os; from pathlib import Path; c=json.loads((Path(os.environ['OUTAGE_STATE_DIR'])/'local-config.json').read_text()); print('\n'.join(role+': '+c['OUTAGE_'+role.upper()+'_PASSWORD'] for role in ('viewer','analyst','admin')))"
```

Do not include that output in shared logs or screenshots. The application does not print passwords during normal startup. [The API contract](API_CONTRACT.md) defines the three personas and their permissions.

A new workspace has accounts but **no analytical publication**. Login and process health can succeed before ingestion; analytical requests need data. No EIA credential or download is required to build the image or start the app.

## Load data explicitly

Supply your own EIA key in the current shell. In PowerShell:

```powershell
$env:EIA_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'EIA API key' -AsSecureString)).Password
```

In Bash:

```bash
read -rsp 'EIA API key: ' EIA_API_KEY
printf '\n'
export EIA_API_KEY
```

Then run a bounded three-day extraction in a separate temporary container:

```sh
docker compose run --rm --no-deps -e EIA_API_KEY app python -m outage_explorer ingest --mode full --start 2026-09-01 --end 2026-09-03 --data-dir /appdata/data
```

`-e EIA_API_KEY` forwards the already-set shell value without putting its literal value in the command. This command needs network access and consumes the provider's API quota; the sample dates are an example, not a claim of fresh live verification. It builds and publishes the matching analytical model before returning. The browser can adopt a newer model with **Use latest**.

To inspect or verify the retained publication without an EIA key:

```sh
docker compose run --rm --no-deps app python -m outage_explorer status --data-dir /appdata/data
docker compose run --rm --no-deps app python -m outage_explorer model verify --data-dir /appdata/data
```

For a later correction-window refresh, replace the ingest options with `--mode recent --data-dir /appdata/data`. Omit `--start` and `--end` from full ingestion only when you intend to download all advertised history. These examples name `--data-dir` explicitly for clarity. The image also uses `/appdata` as its working directory, so the CLI default `data` resolves to the same persistent `/appdata/data`. The CLI itself does not read `OUTAGE_DATA_DIR`. [Local operations](LOCAL_OPERATIONS.md#3-load-or-refresh-source-data) covers extraction semantics and limits.

Remove the key from the shell when finished: `Remove-Item Env:EIA_API_KEY` in PowerShell, or `unset EIA_API_KEY` in Bash. The normal app service is not configured with an EIA key. Runtime environment values remain inspectable by Docker operators; do not bake credentials into an image, Dockerfile or build arguments.

## Run the optional refresh worker

Admin refresh requests enqueue durable jobs. They do not perform ingestion inside the HTTP process. With `EIA_API_KEY` set in your shell as above, enable the separate worker:

```sh
docker compose --profile worker up -d
```

Only the worker receives the Compose-configured EIA key. It shares the same data, application database and generated Django configuration as the app. One worker owns the control lock; additional workers cannot consume concurrently. Without a worker, queued jobs remain queued.

You may clear the shell key after startup; the running worker retains its process environment. Supply the key again before recreating the worker. To stop refresh execution while leaving the app running:

```sh
docker compose stop worker
```

An interrupted job may already have published data because catalog publication and Django job completion are separate transactions. Inspect publication history before retrying. [Refresh operations](LOCAL_OPERATIONS.md#4-execute-admin-refresh-jobs) explains recovery and idempotency.

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
