# Outage Explorer

Outage Explorer turns published U.S. nuclear-outage observations into a local, reproducible analytical workspace. A Python connector downloads the EIA's national, facility and generator data; a verified Parquet model supports an authenticated Django API and browser interface. Public hosting remains future work.

## Documentation

Start with the project tour, then follow the data and request paths. These are the canonical guides; operational commands run from the repository root.

| Guide | Purpose |
| --- | --- |
| [Project tour](docs/PROJECT_TOUR.md) | Product roles, architecture, repository files, generated folders and configuration |
| [Data pipeline and model](docs/DATA_PIPELINE_AND_MODEL.md) | Extraction, publication, metric arithmetic, complete schema and scaling limits; [ER diagram](docs/ER.mmd) |
| [Data findings](docs/FINDINGS.md) | Reconciliation, three concrete anomalies, reproducible queries and interpretation limits |
| [API contract](docs/API_CONTRACT.md) | Authentication, requests/results, pagination, refresh and errors |
| [Query security](docs/QUERY_SECURITY.md) | Authorization, isolated compilation/execution, resource limits and trust boundaries |
| [Browser workspace](docs/BROWSER_WORKSPACE.md) | User flows, product choices, version consistency, sessions, export and accessibility |
| [Engineering decisions](docs/DECISIONS.md) | The seven required challenge choices and all 31 detailed design decisions |
| [Docker workflow](docs/DOCKER.md) | Portable build/startup, private credentials, ingestion, refresh-worker configuration and persistent volumes |
| [Local operations](docs/LOCAL_OPERATIONS.md) | Installation, ingestion, model inspection, reports, checks and recovery |
| [Engineering notes and verification](docs/NOTES.md) | Implementation stages, actual corrections, contribution disclosure, verification and remaining limits |

Test-specific instructions stay with [browser acceptance](tests/browser/README.md), [synthetic fixtures](tests/fixtures/README.md) and [recorded source responses](tests/fixtures/recorded/README.md). Runtime [extension provenance](src/outage_explorer/sandbox/PROVENANCE.md) remains beside its packaged asset.

## Run with Docker

With Docker running Linux containers and the Compose plugin, start from the repository root:

```sh
docker compose up --build -d app
```

Open [the login page](http://127.0.0.1:8000/login/). Initialization generates private random account passwords and persists application state. The app starts its separate continuous refresh worker automatically; no EIA key is needed for startup. A fresh workspace has no analytical data, and refresh requires a key configured only for the worker. Follow [the Docker guide](docs/DOCKER.md) to view passwords privately, load data, configure refresh or move persisted volumes. Local data and credentials are excluded from the image.

## Run without Docker

Use Python **3.12–3.14** and Node **24.x, at least 24.13.0**, with npm. Install both package sets: Django runs in Python, while the server's isolated analytical engine runs in Node. No frontend build step is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
npm ci --ignore-scripts
```

Local datasets and application state are excluded from Git. A fresh checkout needs an extraction before analytical requests can succeed. Supply your own EIA key in the process environment; this bounded example downloads three days:

```powershell
$env:EIA_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'EIA API key' -AsSecureString)).Password
.\.venv\Scripts\python.exe -m outage_explorer ingest --mode full --start 2026-09-01 --end 2026-09-03 --data-dir data
Remove-Item Env:EIA_API_KEY
```

Initialize local accounts and start the loopback service:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer.web.local init
.\.venv\Scripts\python.exe -m outage_explorer.web.local serve --port 8000
```

Open [the local login page](http://127.0.0.1:8000/login/). Initialization creates independent random passwords for `viewer`, `analyst` and `admin` in private ignored `.service/local-config.json`; it does not print them or commit a shared default. Repeating init preserves passwords; `init --reset-passwords` explicitly resets them.

| Account | Access |
| --- | --- |
| Viewer | National overview and only `national_daily` / `fleet_daily`, including permitted SQL |
| Analyst | All ten analytical datasets, filters, previews and supported SQL |
| Admin | Analyst capabilities plus refresh and their own job outcomes |

[.env.example](.env.example) documents variable names. The Python application does not load it or a renamed `.env`; Docker Compose can read `.env` for its own variable interpolation, as described in [the Docker guide](docs/DOCKER.md#ports-paths-and-persisted-state). Local JSON supplies only the generated Django key and three seed passwords; other settings come from the process environment or code defaults. See [configuration precedence](docs/PROJECT_TOUR.md#configuration-what-is-read-and-in-what-order).

Admin refresh queues a durable job. Run its worker separately, with `EIA_API_KEY` supplied in that worker's environment:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer.web.local worker
```

Without a worker, jobs stay queued. Full/recent extraction and offline model/report commands are documented in [local operations](docs/LOCAL_OPERATIONS.md).

## Architecture and operating limits

The connector preserves independent source grains, exact decimals and source provenance. Trusted native DuckDB builds ten analytical datasets; one data-catalog SQLite transaction publishes matching raw/model references after immutable files are ready. Separate Django SQLite state holds users, sessions, jobs and temporary query results.

Django sessions, CSRF and server-derived personas authorize capabilities and every named SQL table before analytical I/O. Submitted SQL passes a bounded disposable compiler, then executes only selected checksummed bytes in a disposable buffer-only DuckDB-WASM worker. Native transforms and reports are trusted application SQL; they use a different execution path.

The browser keeps chart, preview and SQL on one model version and adopts a newer publication explicitly. Exact values remain in tables/API output; null, zero and missing observations retain distinct meanings. Product scope and exclusions are in the [browser guide](docs/BROWSER_WORKSPACE.md).

Recent extraction replaces a 30-day overlap and can miss older revisions; the analytical model is rebuilt fully. EIA's live pages are not a transactional upstream snapshot. SQL is a small single-dataset SELECT subset with 1,000 retained rows, 2 MiB output and an eight-second admitted budget.

An interrupted candidate can remain on disk. There is no automatic orphan cleanup or promotion of unfinished files. Raw/model references commit together, but refresh-job completion is a separate application transaction, so an interrupted job may already have published data. Lock-protected recovery updates abandoned run/build/job labels; it is not proof against every disk/power failure or an off-machine backup. Public hosting, off-machine restore, automatic retention and larger-volume capacity remain unverified.

## Verification status

[CI for `f0efd89`](https://github.com/ricardorodab/arkham-challenge/actions/runs/42206723059), before containerization, passed on Ubuntu and Windows with Python 3.12 and 3.14: each Ubuntu job reported **658 passed, 1 platform skip**; each Windows job reported **657 passed, 2 POSIX skips**. All four jobs passed **17 JavaScript units**, and Windows/Python 3.12 passed **18 browser tests**. The new [Docker checks](docs/DOCKER.md#health-and-verification) separately passed locally on Linux/amd64 with synthetic data; the new container CI job records its own result.

[Engineering notes](docs/NOTES.md) retain earlier scoped records, including the historical 654/1/1 full-suite result and the limited source-documentation checks. [Current Actions runs](https://github.com/ricardorodab/arkham-challenge/actions) and [Docker verification](docs/DOCKER.md#health-and-verification) distinguish later changes and their scope.
