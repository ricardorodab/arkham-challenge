# Part 1 acceptance evidence

This matrix covers **Part 1: Data Connector**. This acceptance record does not claim that Parts 2–4, Django, arbitrary SQL, authentication, or Azure deployment are complete.

Status: the full-history live extraction, real incremental rerun, published-file verification, recorded-response replay coverage, secret audit, and complete local test runs on Python 3.12/3.14 have been completed. Fresh-clone and final repository-state verification remain below. Entries distinguish implementation, actual execution, and remaining checks.

## Core requirements

| PDF requirement | Implementation / test evidence | Final acceptance evidence |
|---|---|---|
| API key read from an environment variable | `cli.main` reads `EIA_API_KEY`; `EIAClient` rejects absent/blank keys. CLI tests require exit `2` before network/storage for a missing key. `.gitignore` excludes local credential inputs. | Live runs used the process environment; actual-key audit passed for Git objects, 44 project files, 10 persisted JSON files, six decompressed Parquet files, and live logs. `key.txt` is ignored and untracked. |
| All three routes extracted, with pagination | `domain.ROUTES`; `EIAClient.fetch_metadata` and `iter_pages`; `pipeline.ingest` requires all three route results. Tests check complete-key sorting, actual-row offsets, misleading totals, empty terminal page, and repeated-page rejection. | Full run `879b99d4cda84084943ba7f448199ea3`: 1,160,019 rows; national 2 nonempty pages, facility 88, generator 143; each route covers 2007-01-01 through 2026-10-30. |
| Required fields validated, with rationale | `domain.validate_row`; explicit schema, date/key/unit/numeric contracts; `tests/test_domain.py`. Rationale in README and DECISIONS. | All published files reopened with their exact recorded schema, valid unique keys and matching counts/hashes. The live run preserved 91 non-positive-capacity flags in each of facility and generator rather than altering source values. |
| Stored locally as Parquet, with logging | `pipeline._route` writes typed ZSTD Parquet; CLI JSON logs use stderr; `Catalog._verify_files` reopens each file. | Full and recent snapshots each contain national/facility/generator Parquet and a manifest. Both passed `verify`; structured live logs passed the actual-key scan. |
| Safe to rerun without duplicates or corruption | Immutable snapshots, OS writer lock, one SQLite publication transaction, per-grain natural keys, duplicate/conflict handling. Pipeline/storage tests cover idempotency, interval replacement, concurrent writers, failures and tampering. | Recent run `981a8f4ed5214ef6be4ad3cc5b4eee01` reread the 30-day overlap, added/changed/deleted zero observations, retained identical logical hashes for all three datasets, and passed verification with no duplicate keys. |
| Deliberate failure behavior documented | README failure matrix and exit codes; EIA typed safe errors; pipeline preserves current snapshot; best-effort diagnostic persistence. | Full suite: 220 passed/one skipped on each of Python 3.12 and 3.14. A genuine HTTP 403 is replayed without retry. Real subprocess death before/after commit, diagnostic I/O failure, corruption, and source failures are covered. |

## Extras implemented and their evidence

| Extra | Implementation / test evidence | Final acceptance evidence |
|---|---|---|
| Stored-watermark incremental extraction | `pipeline._bounds`; per-route watermark minus 29 days; recent-first-run backfill; bounded interval replacement and confirmed deletions. | The real recent run requested 2026-10-01 through 2026-10-30 for every route, from the stored 2026-10-30 watermarks. It preserved all 1,160,019 historical rows and logical hashes. Dedicated tests exercise deletion confirmation and revisions outside the overlap. |
| Rate-aware backoff and bounded parallel extraction | `EIAClient` shared rate lock and cooldown, retry limits and `Retry-After`; pipeline accepts 1–3 workers. Tests use injected clocks and transport. | Full live run: 239 requests, zero retries. Recent live run: 9 requests, zero retries. Offline tests exercise 429/5xx/network retry, HTTP-date and long `Retry-After`, and shared-rate parallel calls; live success is not presented as evidence of retry behavior. |
| Schema evolution handling | `_source_row` retains additive fields; run manifest lists extra field names; required contract and schema version are explicit. Domain/pipeline tests exercise additions, nulls, outliers and breaking changes. | Published schema is verified. Tests retain additive fields, including fields outside a recent replacement interval, while rejecting required-contract changes and lossy values. No incompatible live schema change was claimed. |
| Persisted runs and per-run data quality | `data/catalog.sqlite3` run/snapshot/publication tables; per-route manifest and `data/runs/<run_id>/quality.json`; failure artifacts; interrupted-run recovery. | Reopened history reports both live runs as `succeeded`; full/current verification passed. Full-run DQ has zero duplicate collapses or missing calendar/requested days, flags retained, and the facility declared-total mismatch recorded. `quality.json` says `validated` before commit; SQLite alone determines success. |
| Tests against recorded API responses, including failures | `tests/test_recorded.py` replays genuine metadata/partial data and a genuine HTTP 403. `tests/fixtures/recorded/README.md` and `provenance.json` document scope, recording timestamps and hashes. | Seven actual source recordings: six metadata/first-page responses from smoke run `662caeb500d1433ab55a5df82d403ca8`, plus one separate invalid-credential HTTP 403. Synthetic terminal pages and representative failure fixtures remain explicitly identified as synthetic. |

## Live extraction and repeat evidence

The full-history snapshot is `data/snapshots/879b99d4cda84084943ba7f448199ea3/`. The current repeated snapshot is `data/snapshots/981a8f4ed5214ef6be4ad3cc5b4eee01/`. Both contain a manifest and three verified Parquet files.

| Dataset | Rows | Dates | Logical SHA-256, identical in both snapshots |
|---|---:|---|---|
| National | 7,243 | 2007-01-01–2026-10-30 | `bb5e0433a8ca5c11dbe4f905ef46f49527f3a3b5629eacf354625aacadc51baf` |
| Facility | 438,119 | 2007-01-01–2026-10-30 | `3ed2105fa975f9cdcd7d98f285fbb8b9cd4d9a25d794b0ebc726f6c2e7caceea` |
| Generator | 714,657 | 2007-01-01–2026-10-30 | `10f9a503fb4e3486fe02515e0ab66aca24461781234ae1037d06591ddcd76cb7` |

The full run took 878.913 seconds, with 233 nonempty pages and 239 requests. The recent run took 30.473 seconds, with three nonempty pages and nine requests. Both recorded zero retries. These are measured extraction durations on the implementation machine, not performance guarantees for another machine or future API conditions.

Facility's advertised full-history total was 714,657 versus 438,119 observed rows. The warning is retained. National and generator declared totals matched their observed counts. No diagnostic is substituted for source rows, and no Part 2 reconciliation or explanation of anomalies is claimed.

Local compact evidence is retained in `work/full-evidence.json`, `work/recent-evidence.json`, and `work/secret-audit.json`; authoritative publication state remains in the catalog and snapshot manifests. `work/` and generated datasets are deliberately not committed.

## Safety and recovery checks

The named tests below are directly relevant evidence, subject to a final successful run of the suite:

- `test_parent_corruption_is_rejected_before_new_source_requests`
- `test_deletion_confirmation_must_match_first_extraction`
- `test_failure_reporting_io_does_not_mask_original_error`
- `test_missing_requested_boundary_dates_are_reported`
- `test_transaction_failure_rolls_back_pointer_snapshot_and_success`
- `test_late_error_cannot_downgrade_committed_success`
- `test_writer_lock_excludes_another_process_and_releases`
- `test_recovery_marks_uncommitted_run_without_deleting_evidence`
- `test_parquet_tampering_is_detected`
- `test_manifest_tampering_is_detected_before_serving_current`
- `test_secret_canaries_in_mapping_keys_are_sanitized_recursively`

The Windows staging-file `fsync` correction was followed by successful full and recent live ingestion. The deletion-confirmation and coverage-boundary regression cases passed in the complete suite. Two parameterized cases in `test_actual_process_death_preserves_publication_and_releases_lock` killed real processes after directory rename/before database commit and immediately after database commit; both passed on Python 3.12 and 3.14. Recovery preserved the prior snapshot in the first case and committed success in the second, with the OS lock released.

A source-boundary check does not prove every expected historical entity exists; the limitations remain in README. The Windows symlink case was skipped because the execution account lacked symbolic-link privilege; it is not counted as a passed boundary test. CI includes Linux for that platform-dependent case, but remote CI has not been asserted.

## Reproduction commands

Run from the repository root after the README setup:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m outage_explorer status --data-dir data
.\.venv\Scripts\python.exe -m outage_explorer history --data-dir data --limit 10
.\.venv\Scripts\python.exe -m outage_explorer verify --data-dir data
```

Live ingestion additionally requires `EIA_API_KEY` in the process environment. Do not place the credential in this document, source code, a command argument, a fixture, or Git.

## Final execution record

Recorded completed-command evidence and remaining final checks:

- Final combined suite: Python 3.14 — 220 passed, one skipped, 28.29 seconds; Python 3.12 — 220 passed, one skipped, 31.38 seconds. The single skip in each run is Windows symbolic-link privilege availability; both process-crash cases are included in the 220 passed.
- Ruff lint passed; formatting checks passed for 19 source/test files; `pip check` passed. Fresh-clone execution: pending.
- Full-history and recent run IDs, timing, row counts and fingerprints: completed and recorded above.
- `verify` for both full and current snapshots: completed successfully, including manifest/file hashes, schema, row counts, date coverage, natural keys and logical hashes.
- Recorded-response provenance and actual HTTP 403 replay coverage: completed; see the recorded fixture directory and `tests/test_recorded.py`.
- Secret/credential exclusion scan: passed for all Git objects, 44 project files, 10 persisted JSON files, six Parquet files with decompressed source-row inspection, and full/recent logs. `key.txt` is ignored and untracked. These counts identify the audit scope at execution time.
- Incremental commits exist; final clean tracked working state: pending after documentation and final verification are committed.

No completion claim should be based solely on this matrix's existence or on successful synthetic tests.
