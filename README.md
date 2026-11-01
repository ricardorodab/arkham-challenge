# Outage Explorer — versioned data and a secure Django API

A local, rerunnable connector and analytical model for the three daily EIA nuclear-outage datasets. It discovers source metadata, downloads every page, validates rows, and publishes source data and ten modeled Parquet datasets together as immutable snapshots.

This repository implements **Parts 1–3** of the technical challenge: the connector, verified analytical model and authenticated Django backend. Part 4's web application and the Azure pilot remain future work. See `PART3_ACCEPTANCE.md` for the backend requirement matrix and actual verification evidence.

## Run the backend locally

Use Python 3.12–3.14 and **Node 24.13 or later within the 24.x series**, with npm. Run these commands from the project directory. The existing `data` folder is immediately usable; a fresh clone needs the ingestion described below before analytical requests can succeed.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
npm ci --ignore-scripts
.\.venv\Scripts\python.exe -m outage_explorer.web.local init
.\.venv\Scripts\python.exe -m outage_explorer.web.local serve --port 8000
```

`init` creates the SQLite application database and three real accounts: **viewer**, **analyst**, **admin**. Their independently generated passwords and Django signing key live in ignored `.service/local-config.json`, restricted to the current Windows user or POSIX mode 0600. No default password is committed or printed. Open that private file locally to obtain credentials. Repeating initialization preserves passwords; reset requires explicit `init --reset-passwords`. The web launcher removes EIA and seed-password variables from its environment.

The service listens on **http://127.0.0.1:8000**. This is the backend API; Part 4's login screen and web UI are not included here. In another PowerShell terminal, authenticate and inspect the national catalog:

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

Admin refresh creates a durable job and returns `202` with its status URL. Start a separate worker terminal, supplying `EIA_API_KEY` through the environment as shown in the ingestion instructions below:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer.web.local worker
# Or consume at most one queued job, then exit:
.\.venv\Scripts\python.exe -m outage_explorer.web.local worker --once
```

The API does not start background ingestion threads. Without a worker, jobs remain queued. Poll the returned URL as the same Admin. Refresh preserves the prior published source/model pair on failure and retains historical versions. A worker interrupted after publication can leave a committed version with an interrupted job; its message directs the operator to publication history instead of pretending that nothing changed.

The API contract and examples are in [docs/API.md](docs/API.md); authenticated OpenAPI 3.1 is served at `/api/v1/openapi.json`. [docs/SECURITY.md](docs/SECURITY.md) describes the actual trust boundaries, tested limits and deployment requirements.

## Backend architecture and limits

Session authentication and CSRF use Django. A shared, default-deny policy authorizes the selected dataset and every parsed table reference before analytical data access. Persisted session, account state and role are checked again before delivering or caching results, so logout and revocation during execution take effect.

Submitted SQL is compiled in a bounded Python process, then regenerated SQL runs in a disposable **DuckDB-Wasm** process with a custom buffer-only filesystem. The engine receives only the selected checksummed Parquet bytes, with no application secrets or dataset host path. It cannot read other host files, reach HTTP, load arbitrary extensions, or write files. Native DuckDB 2.0 remains the trusted ingestion/modeling engine; the pinned WASM runtime embeds DuckDB 1.5.4. Parquet compatibility and exact decimal/date serialization are tested across that boundary. The bundled offline Parquet extension has recorded origin, license and SHA-256 in `sandbox/PROVENANCE.md`.

The SQL subset includes one table, filters, projections, arithmetic, ordering, grouping and basic aggregates. JOINs, CTEs, subqueries, UNIONs, windows, table functions, unknown functions and write/configuration statements are rejected. The grammar and engine restrictions complement the WASM boundary; parser validation alone is not the sandbox.

Queries have a **1,000-row total result cap**, **2 MiB response cap**, and shared **eight-second compilation/execution budget**; compilation itself has a two-second ceiling. Windows enforces a hard 512 MiB process limit, with separate WASM/V8 limits. Admission permits two requests executing, one per user, up to eight waiting and five seconds of queue wait; saturation returns `429` with `Retry-After`. POSIX allocation limits are implemented but have not been executed on this Windows machine; hosted Linux acceptance remains necessary before deployment.

Preview uses signed keyset cursors. SQL pagination pages within the capped result and repeats the same SQL/request fields. Both retain one immutable model and bind cursors to user, session, persona, filters/order and limits. The 60-second query cache is also bound to user/session/persona, canonical SQL and model/publication generation; refresh invalidates reuse. Cached results are reauthorized before delivery.

Local HTTP is explicitly limited to loopback. For a hosted configuration use environment-owned secrets, `OUTAGE_LOCAL_HTTP=0`, HTTPS and reviewed host/proxy settings. The local launcher is not an Azure deployment. `outage_explorer.web.manage` exposes ordinary Django management commands for operators who supply configuration directly through environment variables.

## Why these tools

- **Parquet** is the tabular file format required by the challenge. It stores typed columns and compresses repeated data efficiently.
- **DuckDB** reads and writes those files locally. It avoids requiring a database server or a separate Parquet library for this part.
- **SQLite** records local run history, watermarks, and the current snapshot in one transaction. It is operational state, not a second copy of the analytical data. The future deployed service will use PostgreSQL for this responsibility.

The connector uses Python's standard HTTP client. The analytical API uses Django and SQLGlot; it needs no Azure account or database server. Part 2's model and report commands remain offline operations over published local data.

## Install and run

Requirements: Python 3.12 or later, network access to `api.eia.gov`, and an EIA API key for ingestion. Tests and local inspection run without a key.

Direct runtime and development dependencies are pinned in `pyproject.toml`; `requirements.txt` fixes runtime dependencies and `requirements-dev.lock` fixes runtime/test/tool dependencies. `package-lock.json` fixes the isolated WASM engine's npm graph. The implementation environment uses Python 3.14, while the supported minimum is Python 3.12.

From this directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
npm ci --ignore-scripts
.\.venv\Scripts\python.exe -m pytest
```

Supply the key through the **process environment**. This example hides interactive input and does not place the actual key in command history:

```powershell
$env:EIA_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'EIA API key' -AsSecureString)).Password
```

Ingestion and the refresh worker read `EIA_API_KEY`; they do not automatically read `key.txt` or load a `.env` file. `.env.example` documents configuration without containing credentials. The explicit `web.local` launcher reads only its generated local Django/demo configuration. Keep credentials outside version control.

For an initial short extraction that also exercises pagination:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer ingest --mode full --start 2026-09-01 --end 2026-09-03 --page-size 100 --data-dir data
```

To load the complete history advertised by each route's metadata:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer ingest --mode full --data-dir data
```

To update from the stored watermarks with a 30-day overlap:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer ingest --mode recent --data-dir data
```

To inspect local state and verify published files:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer status --data-dir data
.\.venv\Scripts\python.exe -m outage_explorer history --data-dir data --limit 10
.\.venv\Scripts\python.exe -m outage_explorer verify --data-dir data
```

`verify --snapshot-id ID` checks a particular retained snapshot. Inspect `--help` for the command's complete options.

## Build and inspect the analytical model

New ingestions automatically build and validate the model before publishing source and model pointers in the same transaction. If you already have a Part 1 snapshot, or change transformation code, build a new model without network access or an API key:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer model build --data-dir data
.\.venv\Scripts\python.exe -m outage_explorer model status --data-dir data
.\.venv\Scripts\python.exe -m outage_explorer model verify --data-dir data
.\.venv\Scripts\python.exe -m outage_explorer model history --data-dir data --limit 10
.\.venv\Scripts\python.exe -m outage_explorer model catalog --data-dir data --persona viewer
.\.venv\Scripts\python.exe -m outage_explorer model catalog --data-dir data --persona analyst
```

The model contains date/facility/generator dimensions; one fact table for each source grain; `fleet_daily` for US-08; two reconciliation datasets; and deterministic quality events. The complete 113-column contract, 12 foreign keys and ER diagram are in [docs/DATA_MODEL.md](docs/DATA_MODEL.md). Dimension identity comes from the union of observed detail routes, so missing facility-day parents do not discard generators.

US-08 is `national outage MW / national capacity MW` for the same date. The model preserves both source operands and the independently published EIA percentage. It computes the share and percentage using Python `Decimal`, with documented rounding, and returns null plus a quality flag when the denominator is null or nonpositive. It does not average generator percentages or replace the national observation with a child sum.

Reproduce the reconciliation and historical anomaly probes:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer model report --data-dir data --start 2026-09-01 --end 2026-09-30 --output work/part2-report.json
```

The report runs 18 named, packaged SELECT queries, with bound date parameters and query hashes. Windowed reconciliation requires at least 30 calendar days; the historical anomaly probes use their explicit dates. Dates are ISO strings and exact decimal values are JSON strings. Write reports outside the immutable `data` directory. [FINDINGS.md](FINDINGS.md) pins the retained model used for its evidence and contains the command to reproduce that exact version after later refreshes.

For a backend using trusted application-owned SQL, `analytical_catalog(root, persona, model_id=...)` returns logical metadata and metric definitions. `open_model_connection(root, dataset_ids, persona=..., model_id=...)` validates dataset IDs and the persona policy before opening selected Parquet files, pins one model, verifies their file hashes, and registers only those views. Viewer metadata is limited to national/fleet datasets; detail is Analyst/Admin. The CLI persona argument is an operator test input, not authentication. The returned DuckDB connection is trusted code access, not an arbitrary-SQL sandbox or the Part 3 security boundary.

The default ingestion mode is `recent`. With no previously published snapshot, it performs the initial backfill. An explicit `--start` requires `--mode full`; `--end` may constrain either mode. Non-ingestion commands do not require the API key. Before the first publication, `status` reports `current_snapshot: null`, while `verify` returns a storage error because there is no snapshot to verify.

After ingestion, remove the key from the current shell if it is no longer needed:

```powershell
Remove-Item Env:EIA_API_KEY
```

## What is extracted

All routes are under `https://api.eia.gov/v2/nuclear-outages/` and use daily frequency:

| Route | Grain | Natural key |
|---|---|---|
| `us-nuclear-outages` | U.S. fleet per day | `period` |
| `facility-nuclear-outages` | Facility per day | `period`, `facility` |
| `generator-nuclear-outages` | Generator within a facility per day | `period`, `facility`, `generator` |

Metadata is fetched before extracting data. Full extraction uses each route's own source start/end; an explicitly bounded run constrains that source window. Metadata and actual coverage are recorded so that a route lag is visible rather than silently truncating another route.

Whenever the requested interval reaches a metadata-advertised source start or end, that boundary must be present or publication fails as potentially truncated. Other missing dates in a requested interval are reported through `missing_requested_days` and `missing_calendar_days`; they are not invented or filled with zero. These checks improve confidence but cannot establish a transactional source snapshot or prove every historical generator is present.

Requests explicitly select `capacity`, `outage`, and `percentOutage`. Complete-key ordering makes offsets stable for an unchanged source. Offsets advance by **rows actually received**, and extraction continues until an empty page. `response.total` is recorded for diagnosis, not trusted as the sole stopping condition. Repeated or non-progressing pages fail the run.

The EIA API is live while it is paginated. A consistent local snapshot does not mean EIA supplied a simultaneous transaction across all three routes.

## Validation and schema evolution

Required fields were chosen to preserve identity, meaning, and auditability:

| Field | Rule and reason |
|---|---|
| `period` | Required valid daily date; determines the observation and incremental window |
| `facility` / `generator` | Required at their grain and kept as text; generator IDs are scoped to their facility |
| `capacity`, `outage`, `percentOutage` | Fields must exist; nullable values are preserved and flagged rather than converted to zero |
| Metric units | Required and checked against MW / percent so incompatible values cannot be silently combined |
| Names | Descriptive; not natural keys. Missing descriptive values are handled separately from missing identity |

Measures use `DECIMAL(24,9)`. Non-finite, malformed, out-of-range-for-storage, or precision-losing conversions fail publication. Numeric outliers such as negative outage or outage greater than capacity remain source observations with quality flags. An unusual number is not automatically an EIA error.

Identical duplicate observations can be collapsed; conflicting rows with one natural key fail publication. The sanitized source row is preserved for investigation. Additional source fields are retained in source JSON and listed in metadata; missing required fields or incompatible units/types fail closed. The schema is not silently guessed from the first row.

## Safe reruns and publication

1. Acquire a real operating-system writer lock for the data directory. A leftover lock file alone does not mean a process is running.
2. Record the run and its intended per-route windows.
3. Extract and validate all three routes into a unique staging area.
4. Write and reopen Parquet files; record row counts, keys, quality counters, coverage, and file hashes in a manifest.
5. Build, reopen and validate the ten modeled datasets against that immutable source candidate.
6. Commit source/model snapshots, both current pointers, watermarks, and successful run/build statuses in a single SQLite transaction.

No source file in the active snapshot is overwritten. If a route fails or validation fails, the previous snapshot remains current. A process crash can leave an unreferenced staging/snapshot directory; that directory is not automatically treated as published data. A subsequent writer marks abandoned running work as interrupted before starting another run.

The manifest is evidence; the SQLite state is the publication authority. There is no separate `current.json` pointer to get out of sync with run history. `verify` reopens published Parquet and checks its recorded integrity and uniqueness.

Generated files use this layout:

```text
data/
  catalog.sqlite3
  snapshots/<run_id>/
    national.parquet
    facility.parquet
    generator.parquet
    manifest.json
  models/<model_id>/
    <dataset_id>.parquet             # ten datasets, independently checksummed
    manifest.json                   # model/metric version and exact source binding
  runs/<run_id>/
    quality.json
    failure.json                    # only when a failure artifact can be written
    <route>/metadata.json            # with --record-responses
    <route>/page-<offset>.json        # with --record-responses
```

Typed Parquet columns include `_source_row` (sanitized original JSON), `_row_hash`, and `_quality_flags`. These are provenance and validation fields, not derived analytical measures. Verification checks file hashes, schema, row count, natural-key uniqueness, and logical row hashes.

The manifest and `history` retain mode, parent snapshot, extraction/fetch windows, watermark, counts, added/changed/deleted observations, HTTP statistics, schema, extra field names, and per-route quality summaries. The raw manifest's `duration_seconds` measures source extraction through raw-file preparation, before the model build; model-build history records its own start/finish. The `run_succeeded` log measures the complete invocation through joint publication. CLI results go to stdout as JSON; structured logs and errors go to stderr. Logs are not automatically saved as files.

`quality.json` is written with `status: validated` before the publication transaction. That means the files passed validation, **not** that publication committed: use `catalog.sqlite3` through `status`/`history` for authoritative success. A failed or interrupted run receives a failed/interrupted quality record where possible, with completed route summaries and incomplete route names.

Local snapshots protect against a failed refresh; they are **not an off-machine backup**. This part does not implement the later Azure backup/restore policy or automatic snapshot retention. Do not remove snapshots referenced by operational state.

## Incremental refresh and source revisions

For each route, recent extraction starts at its successful watermark minus 29 days, bounded by the source start. It ends at that route's captured metadata end. The successfully fetched interval is replaced, rather than merely upserted, so confirmed source deletions can be represented. Bounded full runs also replace only their requested interval and preserve existing observations outside it. An unbounded full run replaces the complete stored route with the newly extracted history. Unexpectedly empty replacement does not erase existing data.

If observations would be deleted, a second independently paginated extraction must have the same logical content before publication. If the source changes between the two passes, the run fails. In either mode, an advertised source start later than stored coverage or an end behind the stored watermark fails before data requests, rather than silently shrinking history.

The overlap handles recent revisions, not every possible historical correction. EIA exposes no general last-updated timestamp. Run a full extraction periodically to detect revisions outside the overlap; scheduling is not implemented in Part 1. Watermarks advance only when all routes have been successfully published together.

## Requests, logging, and deliberate failures

Extraction is sequential by default. `--workers 3` permits up to three route workers, still governed by a shared global request interval. The default rate is at most one request per second. This is a conservative application policy, not an EIA quota guarantee.

Network failures, HTTP 429, and server failures have bounded retries with backoff. Defaults are a 30-second request timeout and up to three additional attempts. `Retry-After` is honored; a requested delay greater than 60 seconds returns a retryable failure rather than sleeping indefinitely or retrying earlier than requested. Credential and request errors are not retried blindly. The source host and routes are fixed, redirects are rejected, and individual responses are limited to 16 MiB.

| Condition | Behavior |
|---|---|
| Key absent or blank | Reject ingestion before requesting data |
| Invalid credentials / invalid request | Fail clearly without repeated credential attempts |
| Timeout / transient network or 5xx | Retry within the configured bound; preserve current data if exhausted |
| 429 / long `Retry-After` | Respect cooldown or stop with retry information; preserve current data |
| Invalid required field / conflicting duplicate / incompatible schema | Reject publication and retain the previous snapshot |
| Another writer owns the lock | Reject concurrent ingestion without starting competing publication |
| Disk/write/verification failure | Record failure where possible; never switch current to incomplete files |
| Interrupted process | Leave prior committed state authoritative; recover run status on the next writer |

Failed commands return a nonzero exit status. Structured events identify runs/routes and report counts and outcomes. URLs, nested echoed request parameters, saved responses, exceptions, and logs are sanitized. Secrets are never useful provenance.

| Exit status | Meaning |
|---:|---|
| `0` | Command completed successfully |
| `2` | Invalid configuration or command arguments |
| `3` | Source/API failure |
| `4` | Data validation or storage-integrity failure |
| `5` | Another ingestion process owns the writer lock |
| `130` | Interrupted by the operator |

Configuration rejected before a run starts has no ingestion run record. Error recording after a run starts is best effort: if storage itself is unavailable, the original failure remains the reported error rather than being replaced by a secondary failure while writing diagnostics. Ctrl+C marks active work interrupted when it can be recorded; in-flight request threads must unwind within their HTTP limits before process shutdown completes.

`--record-responses` retains sanitized API responses for reproducibility. Recorded responses contain real source data; synthetic test fixtures remain explicitly identified as synthetic. `tests/fixtures/recorded/` contains six genuine metadata/first-page responses and a separately captured actual HTTP 403 failure, with collection scope and SHA-256 provenance. The first pages are partial captures, not complete extracts. Data, operational state, and credentials are ignored by Git.

## Verification and deliverables

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check src tests
.\.venv\Scripts\python.exe -m pip check
```

Offline tests must cover pagination and failure responses, validation, secret canaries, incremental replacement, idempotent reruns, partial failure, concurrent writers, and crash recovery. Live ingestion is a separate acceptance check using a user-supplied key. A green offline suite alone does not prove the remote API was extracted.

Part 1 acceptance evidence consists of a successful three-route live run, published Parquet and manifest, a repeated run without duplicate natural keys, a successful integrity verification, durable run/DQ history, and passing failure tests.

### Historical Part 1 acceptance results

The full-history run `879b99d4cda84084943ba7f448199ea3` published and verified all three routes. These are observed results from this extraction, not a guarantee that EIA never revises them:

| Route | Rows | Nonempty pages | Stored observation dates |
|---|---:|---:|---|
| National | 7,243 | 2 | 2007-01-01 through 2026-10-30 |
| Facility | 438,119 | 88 | 2007-01-01 through 2026-10-30 |
| Generator | 714,657 | 143 | 2007-01-01 through 2026-10-30 |
| **Total** | **1,160,019** | **233** | |

The run made 239 HTTP requests, no retries, and took 878.913 seconds. A recent refresh, `981a8f4ed5214ef6be4ad3cc5b4eee01`, reread 2026-10-01 through 2026-10-30 in 9 requests and 30.473 seconds. It added, changed, and deleted zero observations, preserved all three logical hashes, and passed verification again. Both entries remain in local run history.

The full run reported zero duplicate keys and no missing calendar dates in these stored routes. It preserved 91 `capacity_non_positive` flags in the facility grain and 91 in the generator grain; these counts do not imply distinct events or a diagnosed source error. The facility response declared 714,657 rows while the observed facility extract contained 438,119, which is retained as a metadata-quality warning rather than used to truncate or fabricate data.

The complete local suite passed on both Python 3.14 (221 passed, one skipped; 27.11 seconds) and a fresh checkout on Python 3.12 (221 passed, one skipped; 26.88 seconds). The only skip was Windows symbolic-link privilege availability. Both real-process crash cases, OS writer-lock contention, and path traversal checks passed. Ruff lint, formatting of all 19 source/test files, and dependency consistency checks passed.

See `PART1_ACCEPTANCE.md` for exact fingerprints and completed fresh-checkout verification. A clean checkout without `key.txt` had 221 passing tests and one Windows privilege-dependent skip using the locked dependencies. CI is configured for Windows/Linux and Python 3.12/3.14; configuration is not a claim that a remote GitHub Actions run has occurred.

- `DECISIONS.md`: choices, rejected alternatives, and consequences for this part.
- `NOTES.md`: AI attribution, corrections, and verification record.
- `PART1_ACCEPTANCE.md`: requirement-by-requirement implementation and acceptance evidence.
- `PART2_ACCEPTANCE.md`: model, live refresh, reconciliation and Part 2 verification evidence.
- `PART3_ACCEPTANCE.md`: backend behavior, HTTP acceptance and security/failure verification.
- `docs/API.md`, `docs/SECURITY.md`: API usage and actual authorization/execution boundaries.
- `FINDINGS.md`: three real anomalies, concrete observations, explanations and reproducible queries.
- `docs/DATA_MODEL.md` and `docs/ER.mmd`: typed schema, keys, relationships, metric and scaling policy.

Official references: [EIA API documentation](https://www.eia.gov/opendata/documentation.php), [EIA limits and update behavior](https://www.eia.gov/opendata/faqs.php), [DuckDB Parquet support](https://duckdb.org/docs/stable/data/parquet/overview).
