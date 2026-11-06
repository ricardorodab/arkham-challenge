# Set up, run, inspect and recover the local application

All commands below run from the repository root in PowerShell. They are instructions for a future operator; this documentation rewrite did not execute them. Read [the project tour](PROJECT_TOUR.md) for the file/configuration explanation, [the API contract](API_CONTRACT.md) for request semantics, and [verification](NOTES.md) for dated outcomes rather than assuming these commands currently pass.

For a packaged Linux-container workflow, use [Docker Compose operations](DOCKER.md), including first-run initialization, private credential access, persistent volumes and the optional refresh worker. The PowerShell commands below describe installation directly on the host.

## Choose the work you need

| Goal | Needs EIA key/network? | Changes local state? |
| --- | --- | --- |
| Inspect retained status/history/model/catalog/report | No EIA key | Catalog opening may create workspace folders; report writes its chosen output |
| Start browser/API | No EIA key | Uses Django sessions/cache/application state; needs restored npm runtime for analytics |
| Initialize accounts | No EIA key | Creates private local config, migrates/seeds application database |
| Extract or execute queued refresh | Yes, EIA key in that process | Creates candidates and may publish raw/model versions |
| Build model from retained raw snapshot | No EIA key | Publishes a new model after validation |
| Run tests/browser checks | No EIA key | Creates isolated synthetic state and generated outputs |

Analytical requests require the locked Node packages as well as Python dependencies. A fresh clone excludes generated environments and data; restore or create them with the commands below. Historical query/readiness/browser receipts do not establish current runtime availability.

## 1. Install or restore dependencies

Use Python **3.12–3.14** and Node **24.x, at least 24.13.0**, with npm. Python's virtual environment and Node's package tree serve separate processes. The browser has no build step; Node is needed by the server's isolated analytical engine.

For a fresh checkout:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
npm ci --ignore-scripts
```

To restore missing Node packages, run `npm ci --ignore-scripts`. It restores the lockfile graph without running package lifecycle scripts. Freeze dependency installation while running acceptance: a historical transient query failure overlapped npm writes, although its cause was not proven.

`pyproject.toml` declares direct pinned dependencies and package assets; `requirements.txt` pins runtime/transitive packages; `requirements-dev.lock` includes tests/tools. `package-lock.json` pins npm. The reviewed Python stack pins Django 5.2.18, SQLGlot 30.27.0 and Waitress 3.0.2; the historical review used Python 3.14.2 and Node 24.13.0. Native DuckDB 2.0.0 and WASM's DuckDB 1.5.4 have intentionally different trust roles. Packaged extension provenance remains beside the runtime in [PROVENANCE.md](../src/outage_explorer/sandbox/PROVENANCE.md).

## 2. Initialize accounts and start the loopback server

```powershell
.\.venv\Scripts\python.exe -m outage_explorer.web.local init
.\.venv\Scripts\python.exe -m outage_explorer.web.local serve --port 8000
```

Open [the local login page](http://127.0.0.1:8000/login/). `init` creates the SQLite application database and `viewer`, `analyst`, `admin`. Independent random passwords and the Django signing key are in ignored `.service/local-config.json`, restricted to the current Windows user or POSIX mode 0600. No default password is committed or printed. Read credentials privately, without sharing the file or screenshots. Repeating initialization preserves passwords. **`init --reset-passwords` deliberately resets them; it is not routine startup.**

The local launcher reads exactly its four generated key/password inputs. It does not load `.env` or `.env.example`. Other settings remain inherited process environment or code defaults. The server strips EIA/seed-password variables; the worker strips seed passwords but needs its EIA key. The [configuration table](PROJECT_TOUR.md#configuration-what-is-read-and-in-what-order) explains the wrapper/direct-Django path differences and key precedence.

Viewer sees only national/fleet datasets. Analyst sees all ten. Admin adds refresh and its own job outcomes. The overview, explorer and SQL share one pinned model. Use **Use latest** to adopt a newer publication explicitly. Login rotates CSRF; logout/revocation shields and clears private browser state. This requires JavaScript; the HTML form does not provide a complete non-JavaScript login fallback.

`outage_explorer.web.manage` exposes ordinary Django management commands for an operator supplying environment configuration directly. The local Waitress launcher binds `127.0.0.1`; it is not a cloud deployment. Hosted configuration requires HTTPS, reviewed host/proxy/secret settings and `OUTAGE_LOCAL_HTTP=0`, plus target-platform runtime, restore and capacity acceptance, which remain unverified.

## 3. Load or refresh source data

A fresh clone has no ignored local datasets. Existing retained data can be inspected without downloading again. For ingestion, supply your own key in that process environment; this prompt avoids putting its actual value in command history:

```powershell
$env:EIA_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'EIA API key' -AsSecureString)).Password
```

Choose one extraction scope:

```powershell
# Small initial sample; page-size 100 also exercises pagination.
.\.venv\Scripts\python.exe -m outage_explorer ingest --mode full --start 2026-09-01 --end 2026-09-03 --page-size 100 --data-dir data
# Complete history advertised by each route's metadata.
.\.venv\Scripts\python.exe -m outage_explorer ingest --mode full --data-dir data
# Correction window from stored route watermarks.
.\.venv\Scripts\python.exe -m outage_explorer ingest --mode recent --data-dir data
Remove-Item Env:EIA_API_KEY
```

Default mode is `recent`; without a prior snapshot it backfills. An explicit `--start` requires full mode; `--end` can constrain either. Recent starts at each successful watermark minus 29 days, bounded by source start, and replaces through that route's metadata end. Bounded full replaces only the requested interval; unbounded full replaces all stored history. Proposed deletions require a second independently matching paginated extraction. Empty replacement does not erase existing data. Older corrections can remain undetected until a broader/full extraction. Scheduling periodic extraction is not implemented.

New ingestion builds the **full** analytical model before jointly publishing source/model references. Incremental extraction does not mean incremental model materialization. [Data pipeline/model](DATA_PIPELINE_AND_MODEL.md) owns pagination, schemas, retries, manifests and exact publication guarantees.

## 4. Execute Admin refresh jobs

Admin's POST queues a durable job and returns `202` with its status URL; it does not download inside the HTTP request. Start a separate terminal with `EIA_API_KEY` in that terminal's environment, then:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer.web.local worker
# Alternatively, consume at most one queued job and exit.
.\.venv\Scripts\python.exe -m outage_explorer.web.local worker --once
```

Without a worker, jobs remain queued. The same Admin can poll its returned status URL. Idempotency keys make uncertain retry outcomes recoverable; see the [exact refresh contract](API_CONTRACT.md#asynchronous-refresh). Database uniqueness, worker control locking and ingestion writer locking protect different concurrency boundaries. Revocation before execution prevents start; changing the Admin after extraction starts does not cancel extraction.

Validation/extraction failures **before** publication leave the previous pair current. Catalog publication and application-job completion are separate transactions. If interrupted, inspect both authoritative histories: a job marked interrupted may already have published data. Do not promise exactly-once refresh or infer no change from job status alone.

## 5. Inspect and reproduce retained results

These commands need no EIA key:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer status --data-dir data
.\.venv\Scripts\python.exe -m outage_explorer history --data-dir data --limit 10
.\.venv\Scripts\python.exe -m outage_explorer verify --data-dir data
.\.venv\Scripts\python.exe -m outage_explorer model status --data-dir data
.\.venv\Scripts\python.exe -m outage_explorer model history --data-dir data --limit 10
.\.venv\Scripts\python.exe -m outage_explorer model verify --data-dir data
.\.venv\Scripts\python.exe -m outage_explorer model catalog --data-dir data --persona viewer
.\.venv\Scripts\python.exe -m outage_explorer model catalog --data-dir data --persona analyst
```

`verify --snapshot-id ID` checks a retained raw snapshot. Before any publication, status reports `current_snapshot: null`; verify fails with a storage error because nothing is published. The CLI `--persona` is a trusted operator metadata filter, not authentication. `--help` lists complete options.

To build a new model from an existing raw snapshot after a transformation change, without network access:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer model build --data-dir data
```

This mutates model publication; it is distinct from inspection. A stale-source guard prevents attaching a model to an incorrectly advanced source.

To reproduce the retained findings' analytical values with the historical model (current SQL comments can produce different query-text hashes):

```powershell
.\.venv\Scripts\python.exe -m outage_explorer model report --data-dir data --model-id e5721cad365d47beb0a8a322199a3138 --start 2026-09-01 --end 2026-09-30 --output work/part2-demo-report.json
```

Omit `--model-id` to analyze the selected current model; a fresh extract will have different IDs. Reports execute 18 named packaged SELECTs with bound window dates, hashes, columns and provenance. Reconciliation needs at least 30 calendar days; historical anomaly probes retain explicit dates. Decimals are JSON strings and dates are ISO strings. Write outputs outside immutable data directories. `work/` here is a **future output**, not the archived evidence location. [Findings](FINDINGS.md) and [engineering notes](NOTES.md) distinguish the original pinned bundle from later observations. Private historical artifacts are not part of a fresh clone.

The trusted Python adapter `analytical_catalog(...)`/`open_model_connection(...)` validates persona/dataset IDs, pins one model, verifies hashes and registers only selected views. It is application-owned native DuckDB access, not the hostile-user SQL boundary; see [query security](QUERY_SECURITY.md).

## 6. Exercise the API from PowerShell

The same server exposes `/api/v1`. In another PowerShell terminal, you can authenticate and inspect the national catalog directly:

```powershell
$localConfig = Get-Content -LiteralPath .service/local-config.json -Raw | ConvertFrom-Json
$webSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$csrf = Invoke-RestMethod http://127.0.0.1:8000/api/v1/auth/csrf -WebSession $webSession
$loginBody = @{ username = 'viewer'; password = $localConfig.OUTAGE_VIEWER_PASSWORD } | ConvertTo-Json
$login = Invoke-RestMethod http://127.0.0.1:8000/api/v1/auth/login -Method Post -WebSession $webSession -ContentType application/json -Headers @{ 'X-CSRFToken' = $csrf.csrf_token } -Body $loginBody
Invoke-RestMethod http://127.0.0.1:8000/api/v1/catalog -WebSession $webSession
$queryBody = @{ dataset_id = 'fleet_daily'; sql = 'SELECT period, offline_pct FROM fleet_daily ORDER BY period DESC LIMIT 10' } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/api/v1/query -Method Post -WebSession $webSession -ContentType application/json -Headers @{ 'X-CSRFToken' = $login.csrf_token } -Body $queryBody
```

Login rotates the CSRF token; use the returned token for later POSTs. All four capabilities require a server-derived persona. Viewer sees only national/fleet datasets; Analyst sees all analytical datasets; Admin adds refresh. No request body, header, staff flag or superuser flag selects a persona.


Use the [API contract](API_CONTRACT.md) for all endpoints, SQL examples, pagination, model pins, errors and limits. Unknown and forbidden datasets both return 403 before analytical I/O. Readiness resolves model/runtime availability and rechecks identity; it does not prove a full engine query will succeed.

## 7. Diagnose failures and preserve evidence

CLI JSON goes to stdout; structured logs/errors go to stderr. Logs are not automatically saved. `quality.json` with `status: validated` means file validation passed **before** commit; only catalog status/history establishes published success. Raw manifest duration stops before modeling; model history has its own timing; `run_succeeded` measures the complete joint invocation. Optional `--record-responses` stores sanitized actual source responses; partial captures are not complete extracts.

| Failure | Expected behavior / operator meaning |
| --- | --- |
| Missing/blank key or invalid arguments | Reject before requesting data; configuration rejected before start has no ingestion run record |
| Credential/request error | Fail without blind credential retries |
| Network/timeout/5xx or 429 | Bounded retry/backoff; respect Retry-After or return retry information; preserve current publication when exhausted |
| Missing required field, bad type/unit, conflicting duplicate or precision loss | Reject candidate; retain prior published pair |
| Suspicious finite measurement | Preserve with quality flags when representable; do not silently repair it |
| Writer lock owned | Reject competing ingestion; a leftover lock file alone does not establish an owner |
| Bounded compiler unavailable (`503`) | Check the trusted interpreter/package configuration, child protocol and remaining resource/deadline budget without printing SQL or secrets |
| Disk/write/verification failure | Best-effort diagnostics; do not replace original error with a secondary logging failure; never publish incomplete files |
| Process interruption | Prior committed references remain authoritative; next writer recovers abandoned run/build status; unreferenced candidates may remain |

| Exit code | Meaning |
| ---: | --- |
| 0 | Success |
| 2 | Invalid configuration/arguments |
| 3 | Source/API failure |
| 4 | Validation/storage integrity failure |
| 5 | Another ingestion writer owns the lock |
| 130 | Operator interruption |

Requests default to sequential, at most one/second; `--workers 3` allows up to three route workers under the same shared rate gate. Defaults are 30-second request timeout and three additional attempts. A Retry-After above 60 seconds fails retryably rather than sleeping indefinitely or retrying early. Individual responses are capped at 16 MiB; host/routes are fixed and redirects refused. Individual request/page/retry limits do not create a strict whole-ingestion deadline. Ctrl+C is cooperative; in-flight threads unwind within HTTP bounds.

## 8. Recovery, backup and storage limits

Inspect catalog status/history first. Prepared files, manifests, staging directories and `validated` records are not independent publication authorities. Do not hand-edit current pointers or promote leftover candidates. Real process-death tests distinguish raw rename, model rename, precommit failure and committed success; their dated scope is in [verification](NOTES.md).

Old immutable snapshots protect against a failed refresh but are **not off-machine backups**. Recovery does not automatically promote unfinished candidates. No automatic reference-aware retention, orphan cleanup or proven remote restore exists. Unfinished candidates can remain after process interruption; catalog references, not directory discovery, determine what is published. Do not remove snapshots/models referenced by operational state, jobs, pinned findings or active cursors. A future policy must account for those references and validate restore in an empty environment. The historical target was RPO 24 hours/RTO two hours; neither is demonstrated.

For a consistent backup, coordinate writers and use a SQLite-aware backup procedure or an appropriately quiesced copy of each database and its referenced immutable files. A live WAL database can have committed state in its `-wal`; `-shm` is coordination state. Do not delete helper files or assume copying only `*.sqlite3` captures committed state. There is no atomic cross-database backup/restore procedure claimed here. Process-crash checks and SQLite durability settings do not prove universal power-loss recovery. Hosted storage/backup acceptance remains future work.

Use short writable scratch paths for verification: the November 2 environment could write a 243-character staging path but failed at 261 characters. The short-root rerun removed setup cascades, leaving the separate timeout-test failure. Do not overwrite historical receipts; select new output paths.

## 9. Run checks when runtime dependencies are restored

The latest full Python result is 654 passed, 1 failed, 1 skipped (November 2); JavaScript units were 17 passed (November 3). These commands were **not run for this rewrite**:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check src tests
.\.venv\Scripts\python.exe -m pip check
npm run test:unit
npx playwright install chromium
npm run test:browser
node tests/browser/summarize.cjs
```

Browser tests start a separate `DEBUG=False` Waitress service on port 8766 with synthetic data/state/test-only credentials. Keep the port free. Chromium uses one worker and zero retries. Real success paths execute the backend and separate refresh worker; some controlled failure states use interception. Outputs intentionally go to `work/browser-results/` and `work/browser-report/`; `npx playwright show-report work/browser-report` opens future results. `OUTAGE_TEST_PYTHON` selects another installed interpreter. See [browser test instructions](../tests/browser/README.md), [synthetic fixtures](../tests/fixtures/README.md) and [recorded-response provenance](../tests/fixtures/recorded/README.md).

Offline tests do not prove new EIA extraction, remote CI, hosted Linux resource behavior or assistive-technology usability. The recorded scope of historical browser checks, failed attempts, clean checkouts and wheels is described in [engineering notes](NOTES.md); private supporting artifacts are not distributed with the source. Never turn an old green stage run into a claim that the latest full suite is green.

## Source documentation retained from the earlier guides

These references supported the original design; they were not revalidated online for this rewrite: [EIA API documentation](https://www.eia.gov/opendata/documentation.php), [EIA limits and update behavior](https://www.eia.gov/opendata/faqs.php), [DuckDB Parquet support](https://duckdb.org/docs/stable/data/parquet/overview), [DuckDB numeric types and division](https://duckdb.org/docs/2.0/sql/data_types/numeric).
