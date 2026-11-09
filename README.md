# Outage Explorer

Outage Explorer turns published U.S. nuclear-outage observations into a reproducible analytical workspace. A Python connector downloads the EIA's national, facility and generator data; a verified Parquet model supports an authenticated Django API and browser interface. Run it locally or deploy the containerized application to one persistent Linux VM.

[Workspace preview](docs/screenshots/overview-desktop.png) · [Data model diagram](docs/ER.svg)

## Documentation

Start with the project tour, then follow the data and request paths. These are the canonical guides; operational commands run from the repository root.

| Guide | Purpose |
| --- | --- |
| [Project tour](docs/PROJECT_TOUR.md) | Product roles, architecture, repository files, generated folders and configuration |
| [Data pipeline and model](docs/DATA_PIPELINE_AND_MODEL.md) | Extraction, publication, metric arithmetic, complete schema and scaling limits; [ER diagram](docs/ER.svg) |
| [Data findings](docs/FINDINGS.md) | Reconciliation, three concrete anomalies, reproducible queries and interpretation limits |
| [API contract](docs/API_CONTRACT.md) | Authentication, requests/results, pagination, refresh and errors |
| [Query security](docs/QUERY_SECURITY.md) | Authorization, isolated compilation/execution, resource limits and trust boundaries |
| [Browser workspace](docs/BROWSER_WORKSPACE.md) | User flows, product choices, version consistency, sessions, export and accessibility |
| [Engineering decisions](docs/DECISIONS.md) | Recorded choices, rejected alternatives and engineering trade-offs |
| [Browse performance](docs/BROWSE_PERFORMANCE.md) | Three-million-row synthetic measurements, scope and reproduction |
| [Docker workflow](docs/DOCKER.md) | Portable build/startup, private credentials, ingestion, refresh-worker configuration and persistent volumes |
| [VM deployment](deploy/README.md) | HTTPS hosting, GitHub Actions releases, persistent state, rollback and operator configuration |
| [Security policy](SECURITY.md) | Protected changes, scanning, maintainer access and private vulnerability reporting |
| [Backup and restore](deploy/backup-restore.md) | Encrypted off-host recovery, retention, key ownership and isolated restore drills |
| [Local operations](docs/LOCAL_OPERATIONS.md) | Installation, ingestion, model inspection, reports, checks and recovery |
| [Engineering notes and verification](docs/NOTES.md) | Implementation stages, actual corrections, contribution disclosure, verification and remaining limits |

Test-specific instructions stay with [browser acceptance](tests/browser/README.md), [synthetic fixtures](tests/fixtures/README.md) and [recorded source responses](tests/fixtures/recorded/README.md). Runtime [extension provenance](src/outage_explorer/sandbox/PROVENANCE.md) remains beside its packaged asset.

Application code is grouped by responsibility: [`data/`](src/outage_explorer/data/) for ingestion/publication, [`analytics/`](src/outage_explorer/analytics/) for modeling/reports, and [`query/`](src/outage_explorer/query/) for query validation/execution. The existing `frontend/`, `service/`, `web/` and `sandbox/` folders retain their roles. See the [repository map](docs/PROJECT_TOUR.md#what-each-root-folder-and-configuration-file-does) for the full reading guide.

## Run with Docker

For **local testing**, use Docker running Linux containers and the Compose plugin, and work from the repository root. **For live data or Admin refresh, set your own EIA API key in the same terminal before the first `docker compose up`.** In PowerShell:

```powershell
$env:EIA_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'EIA API key' -AsSecureString)).Password
docker compose up --build -d app
```

Open [the login page](http://127.0.0.1:8000/login/). Compose initializes private random account passwords and starts the app plus its separate refresh worker. Only the worker receives the key. [View your generated passwords privately](docs/DOCKER.md#start-a-fresh-workspace). A fresh workspace has no analytical data: [load a bounded initial dataset](docs/DOCKER.md#load-data-explicitly), or request a refresh as Admin and choose **Use latest** after it succeeds.

For login or reading existing data only, you may omit the key prompt; keyless startup is valid, but live extraction and refresh need the key. [Bash setup and key sources](docs/DOCKER.md#before-startup-configure-live-data-access) are documented separately. **Already started without the key?** Leave the app running, set the key, then [recreate only the worker](docs/DOCKER.md#already-started-without-the-key); a simple restart does not update its environment. Local data and credentials are excluded from the image.

## Deploy from GitHub

Open a pull request for changes to the protected `main` branch. Required CI and security
checks must pass before merging; direct pushes, force pushes and branch deletion are blocked.
Merges to `main` run the Python, JavaScript, browser and Docker checks. After all checks
pass, GitHub Actions deploys the accepted image digest to the configured Azure VM and
verifies its Git revision over HTTPS. Superseded commits and pull requests do not deploy.
Accounts, password hashes, saved views and datasets remain on persistent VM storage across
releases. Production secrets come from the private Azure Key Vault through the VM's managed
identity and are mounted as read-only, role-specific temporary files. Secret values are not
supplied through container environment variables, and production does not load secret `.env`
files. The existing Django signing key is preserved. An active refresh defers an upgrade;
rerun the failed deployment job after the refresh finishes.
See [VM deployment](deploy/README.md) for identity permissions, configuration and recovery.

## Run without Docker

For **local testing**, use Python **3.12–3.14** and Node **24.x** with npm; CI currently verifies Node **24.22.2**, also pinned in the production image. Install both package sets: Django runs in Python, while the server's isolated analytical engine runs in Node. The normal setup uses the checked-in frontend assets; rebuilding the editor or translations is a separate development operation.

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

## Test users

Open [the local login page](http://127.0.0.1:8000/login/). Local initialization creates independent random passwords for `viewer`, `analyst` and `admin` in private ignored `.service/local-config.json`; it does not print them or commit a shared default. Docker users can [retrieve their generated passwords privately](docs/DOCKER.md#start-a-fresh-workspace).

Ordinary init applies migrations and preserves account edits, including renamed/deleted demo users. `init --reset-passwords` explicitly reseeds default identities for operator recovery, subject to conflict checks. Admins manage accounts through **Users**; generated passwords are shown once and do not update the initialization configuration.

| Account | Access |
| --- | --- |
| `viewer` | National overview; preview/query only `national_daily` and `fleet_daily`; consume permitted shared views |
| `analyst` | All ten analytical datasets, filters, previews, supported SQL, and creation/sharing of saved tables and charts |
| `admin` | Analyst capabilities plus user management, refresh and their own job outcomes |

Production has separate initial account secrets in Key Vault. Those bootstrap values are not a record of current user passwords: later account changes remain in the application database as hashes.

## Local configuration and refresh worker

[.env.example](.env.example) documents variable names. The Python application does not load it or a renamed `.env`; Docker Compose can read `.env` for its own variable interpolation, as described in [the Docker guide](docs/DOCKER.md#ports-paths-and-persisted-state). Local JSON supplies only the generated Django key and three seed passwords; other settings come from the process environment or code defaults. See [configuration precedence](docs/PROJECT_TOUR.md#configuration-what-is-read-and-in-what-order).

Admin refresh queues a durable job. Run its worker separately, with `EIA_API_KEY` supplied in that worker's environment:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer.web.local worker
```

Without a worker, jobs stay queued. Full/recent extraction and offline model/report commands are documented in [local operations](docs/LOCAL_OPERATIONS.md).

## Architecture

The connector preserves independent source grains, exact decimals and source provenance. Trusted native DuckDB builds ten analytical datasets; one data-catalog SQLite transaction publishes matching raw/model references after immutable files are ready. Separate Django SQLite state holds users, sessions, jobs and temporary query results.

Django sessions, CSRF and server-derived personas authorize capabilities and every named SQL table before analytical I/O. Submitted SQL passes a bounded disposable compiler, then executes only selected checksummed bytes in a disposable buffer-only DuckDB-WASM worker. Native transforms and reports are trusted application SQL; they use a different execution path.

Explore uses a separate bounded native DuckDB reader for full-dataset search, typed filtering, sorting and seek pagination. It returns a page without copying the complete dataset into the browser or WASM engine. The [browse benchmark](docs/BROWSE_PERFORMANCE.md) exercised three million synthetic rows on Windows and Linux Docker; it is not a concurrent-user load test or a production latency guarantee.

The browser keeps chart, preview and SQL on one model version and adopts a newer publication explicitly. Exact values remain in tables/API output; null, zero and missing observations retain distinct meanings. Product scope and exclusions are in the [browser guide](docs/BROWSER_WORKSPACE.md).

## Assumptions

- Production runs on one persistent Linux VM. Web and worker processes share the same SQLite databases, analytical files and operating-system locks; host storage survives ordinary application releases.
- A fresh checkout has no analytical data. Live extraction needs a valid EIA key and network access; reading an existing publication does not need live EIA access.
- EIA publishes independent, revisable source grains. Reported totals and percentages are evidence to validate and reconcile, not values the model forces into agreement.
- Product users access data through Django authorization. VM/root/Docker operators and approved deployment code are trusted; a product Admin is not an infrastructure administrator.
- Production startup requires private DNS, connectivity and managed-identity access to Key Vault. Existing published files are immutable and retained while readers may still reference them; users adopt a new publication explicitly.

## Known limitations

- **Hosting and capacity:** the current storage and locks coordinate one host. Horizontal autoscaling, high availability and sustained concurrent-user capacity are not established by the synthetic browse benchmark.
- **Extraction:** recent mode replaces a 30-day overlap and can miss older revisions. Every refresh rebuilds the full analytical model. EIA's live pages are not a transactional snapshot, and periodic data refresh is not scheduled automatically.
- **SQL workspace:** queries use one dataset and a restricted read-only SELECT language; joins, writes and arbitrary SQL are unsupported. Execution retains at most 1,000 rows, permits 2 MiB output, shares an eight-second admitted budget, and has a separate 64 MiB buffered-input ceiling. Structured Explore browsing follows its own bounded reader path.
- **Recovery:** interrupted candidates may remain on disk; there is no automatic orphan cleanup or unfinished-file promotion. Catalog publication and application-job completion are separate transactions, so an interrupted job may already have published data. The [encrypted backup/restore tools](deploy/backup-restore.md) are implemented and tested, but off-host storage and backup timers remain inactive in the current deployment. Process-crash recovery is not a guarantee against every disk or power failure.
- **Refresh requests:** only one job may be queued or running globally. Competing requests normally receive `409 refresh_busy`; a known completion race in the [enqueue error handler](src/outage_explorer/service/refresh.py) can instead return `500` if the competing job finishes between checks. The database still prevents two active jobs; this error-classification defect remains open.
- **Secret availability:** a cold start depends on Key Vault and its private network path. There is no persistent plaintext fallback. VM/root/Docker administrators can read runtime secrets, and moving a key into the vault does not rotate its value.

## Verification status

The verified runtime baseline is commit [`2c55a67`](https://github.com/ricardorodab/arkham-challenge/commit/2c55a67c06ad7d47986d1152f4a20e02b5a3981c). Its [CI and automatic Azure deployment](https://github.com/ricardorodab/arkham-challenge/actions/runs/42558242450) completed successfully:

| Check | Recorded result |
| --- | --- |
| Ubuntu, Python 3.12 and 3.14 | 1,164 passed, 1 platform skip per job |
| Windows, Python 3.12 and 3.14 | 1,144 passed, 21 platform skips per job |
| JavaScript, editor and localization checks | Passed |
| Browser acceptance | 49 passed on Windows/Python 3.12 |
| Docker acceptance | Standard application flow plus production secret isolation and restart recovery passed |
| Security | Dependency audits, Git secret scan, container security gate and CodeQL passed |
| Deployment | Accepted image digest deployed; exact Git revision verified over HTTPS |

These are results for the linked commit and tested platforms, not a guarantee for future changes or every deployment target. Live cutover also verified preserved account/password state, the unchanged Django key, worker EIA access, blocked container identity endpoints and the absence of legacy server secret files. Consult [current Actions runs](https://github.com/ricardorodab/arkham-challenge/actions) for later revisions, [Docker verification](docs/DOCKER.md#health-and-verification) for acceptance scope, and [engineering notes](docs/NOTES.md) for earlier implementation records.
