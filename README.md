# Outage Explorer — Part 1: EIA data connector

A local, rerunnable connector for the three daily EIA nuclear-outage datasets. It discovers source metadata, downloads every page, validates rows, and publishes the three Parquet datasets together as an immutable snapshot.

This repository currently implements **Part 1** of the technical challenge. Django, the web application, user authorization, arbitrary SQL, and Azure deployment belong to later parts and are not prerequisites for running this connector.

## Why these tools

- **Parquet** is the tabular file format required by the challenge. It stores typed columns and compresses repeated data efficiently.
- **DuckDB** reads and writes those files locally. It avoids requiring a database server or a separate Parquet library for this part.
- **SQLite** records local run history, watermarks, and the current snapshot in one transaction. It is operational state, not a second copy of the analytical data. The future deployed service will use PostgreSQL for this responsibility.

The connector uses Python's standard HTTP client. It does not need Django, SQLGlot, Azure credentials, or a running external database.

## Install and run

Requirements: Python 3.12 or later, network access to `api.eia.gov`, and an EIA API key for ingestion. Tests and local inspection run without a key.

Direct runtime and development dependencies are pinned in `pyproject.toml`; `requirements.txt` provides the pinned DuckDB runtime dependency, and `requirements-dev.lock` fixes the direct and transitive test/tool dependencies. The implementation environment uses Python 3.14, while the supported minimum is Python 3.12.

From this directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pytest
```

Supply the key through the **process environment**. This example hides interactive input and does not place the actual key in command history:

```powershell
$env:EIA_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'EIA API key' -AsSecureString)).Password
```

The application reads `EIA_API_KEY`; it does not automatically read `key.txt` or load a `.env` file. `.env.example` documents configuration without containing credentials. Keep keys outside version control.

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
5. Publish immutable files and commit the snapshot, current pointer, watermarks, and successful run status in a single SQLite transaction.

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
  runs/<run_id>/
    quality.json
    failure.json                    # only when a failure artifact can be written
    <route>/metadata.json            # with --record-responses
    <route>/page-<offset>.json        # with --record-responses
```

Typed Parquet columns include `_source_row` (sanitized original JSON), `_row_hash`, and `_quality_flags`. These are provenance and validation fields, not derived analytical measures. Verification checks file hashes, schema, row count, natural-key uniqueness, and logical row hashes.

The manifest and `history` retain mode, parent snapshot, extraction/fetch windows, watermark, counts, added/changed/deleted observations, HTTP statistics, schema, extra field names, and per-route quality summaries. CLI results go to stdout as JSON; structured logs and errors go to stderr. Logs are not automatically saved as files.

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

### Observed live acceptance results

The full-history run `879b99d4cda84084943ba7f448199ea3` published and verified all three routes. These are observed results from this extraction, not a guarantee that EIA never revises them:

| Route | Rows | Nonempty pages | Stored observation dates |
|---|---:|---:|---|
| National | 7,243 | 2 | 2007-01-01 through 2026-10-30 |
| Facility | 438,119 | 88 | 2007-01-01 through 2026-10-30 |
| Generator | 714,657 | 143 | 2007-01-01 through 2026-10-30 |
| **Total** | **1,160,019** | **233** | |

The run made 239 HTTP requests, no retries, and took 878.913 seconds. A recent refresh, `981a8f4ed5214ef6be4ad3cc5b4eee01`, reread 2026-10-01 through 2026-10-30 in 9 requests and 30.473 seconds. It added, changed, and deleted zero observations, preserved all three logical hashes, and passed verification again. Both entries remain in local run history.

The full run reported zero duplicate keys and no missing calendar dates in these stored routes. It preserved 91 `capacity_non_positive` flags in the facility grain and 91 in the generator grain; these counts do not imply distinct events or a diagnosed source error. The facility response declared 714,657 rows while the observed facility extract contained 438,119, which is retained as a metadata-quality warning rather than used to truncate or fabricate data.

The complete local suite passed on both Python 3.14 (220 passed, one skipped; 28.29 seconds) and Python 3.12 (220 passed, one skipped; 31.38 seconds). The only skip was Windows symbolic-link privilege availability. Both real-process crash cases, OS writer-lock contention, and path traversal checks passed. Ruff lint, formatting of all 19 source/test files, and dependency consistency checks passed.

See `PART1_ACCEPTANCE.md` for exact fingerprints and remaining fresh-clone/final repository checks. CI is configured for Windows/Linux and Python 3.12/3.14; configuration is not a claim that a remote GitHub Actions run has occurred.

- `DECISIONS.md`: choices, rejected alternatives, and consequences for this part.
- `NOTES.md`: AI attribution, corrections, and verification record.
- `PART1_ACCEPTANCE.md`: requirement-by-requirement implementation and acceptance evidence.

Official references: [EIA API documentation](https://www.eia.gov/opendata/documentation.php), [EIA limits and update behavior](https://www.eia.gov/opendata/faqs.php), [DuckDB Parquet support](https://duckdb.org/docs/stable/data/parquet/overview).
