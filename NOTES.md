# Engineering notes — Part 1

## Attribution and scope

The connector, tests, and supporting documents were authored with AI assistance in this Codex chat. The user supplied the challenge and API credential, selected the overall product direction, and requested a high-quality Part 1 implementation. No code should be attributed to manual user authorship without evidence.

This delivery is the data connector only; it does not claim to implement the Django application, SQL sandbox, role authorization, Azure deployment, or Part 2 findings.

## A concrete AI design correction

The earlier full-product proposal relied too heavily on SQLGlot validation and DuckDB configuration to isolate user SQL. Reviewing DuckDB's primary security documentation showed that those settings are defense in depth, not a replacement for operating-system isolation. The revised plan requires an isolated per-query runner with limited data and resources. That future capability is intentionally absent from Part 1, which executes only application-owned data-processing SQL.

This correction illustrates why a plausible generated design is not sufficient evidence of security. Source documentation, a stated threat model, and tests of the intended boundary are necessary.

## Concrete implementation errors caught

1. **Windows file synchronization:** the AI initially called `os.fsync` on a Parquet file opened with `rb`. Windows rejected that descriptor with `OSError: [Errno 9] Bad file descriptor`. A real three-route smoke extraction and the pipeline integration tests exposed the same problem. The file is now opened with `r+b` only while it is still in staging, synchronized, and closed before publication; published snapshots are never reopened for writing. The later successful full-history and recent live publications verified this path after the correction.
2. **Failure reporting hiding the real failure:** attempting to persist a diagnostic artifact could itself fail and replace the original typed API/data error. A regression test injects reporting I/O failure. Recording status and artifacts is now best effort, preserving the primary failure and the previous publication.
3. **Coverage validation:** pagination ending with an empty page did not establish that an advertised source boundary had been received. A guard now rejects missing metadata-advertised boundaries and records other missing requested dates. Review also caught an indentation mistake while adding that guard: deletion-confirmation comparison had become unreachable. The comparison was moved back to its own deletion branch, with a regression test that requires the confirmation contents to match.

4. **Fixture bytes after Git checkout:** the first clean checkout failed the recorded-response hash test because a Windows-created HTTP 403 fixture used CRLF while Git stored LF. The working directory alone had passed. The fixtures and provenance were normalized to UTF-8/LF, and a regression test now checks portable serialization. This was a fixture-provenance problem, not a change to downloaded Parquet or source values; the corrected clean checkout passed its complete suite.

These are errors in generated work, not hypothetical defects. Named regression tests and live reruns provide distinct evidence; the existence of a fix alone is not proof.

## Verification approach

- Re-read Part 1 and distinguish its core requirements and extras from later product work.
- Inspect actual EIA metadata rather than guessing field names and units.
- Test pagination independently of the declared source total.
- Use malformed responses and injected failures to prove that publication preserves the prior valid snapshot.
- Exercise stored-watermark refresh, interval replacement, duplicate handling, and repeated ingestion.
- Test nested secret canaries against generated responses, logs, and artifacts.
- Verify real Parquet schema, row counts, natural-key uniqueness, and file hashes.
- Run a live extraction separately from offline fixtures, then repeat and verify it.
- Keep commits incremental and retain ordinary fixes instead of rewriting history.

Commands and actual results should be read together: an offline test suite does not prove a live download completed, and the existence of a Parquet file does not prove every source page was extracted.

## Known limits to retain in review

- EIA updates while offset pagination is in progress and provides no general last-updated field. A local publication is coherent; it is not an atomic source snapshot.
- A 30-day overlap cannot detect every older revision. Full extraction remains necessary.
- Semantic outliers are preserved; Part 1 does not claim to explain three real anomalies or reconcile the source grains.
- Snapshots and local operational history are not off-machine backup or an availability guarantee.
- Source/API behavior must be documented from actual evidence, and synthetic fixtures must not be described as recorded source responses.

## Evidence record

The requirement matrix is in `PART1_ACCEPTANCE.md`. Recorded live evidence:

- Full backfill `879b99d4cda84084943ba7f448199ea3`: 1,160,019 rows across all three grains, each covering 2007-01-01 through 2026-10-30; 233 nonempty pages, 239 HTTP requests, zero retries, 878.913 seconds; published files passed verification.
- Recent refresh `981a8f4ed5214ef6be4ad3cc5b4eee01`: each route reread 2026-10-01 through 2026-10-30; 9 HTTP requests, 30.473 seconds, no added/changed/deleted rows, unchanged logical hashes; published files passed verification again.
- Real source fixtures: six metadata/partial-page responses from a recorded smoke attempt, plus an independently recorded actual HTTP 403 response. Their provenance deliberately records that the smoke attempt failed before publication; those fixtures alone do not prove extraction success.
- The final suite passed on Python 3.14: 221 passed, one skipped, 27.11 seconds; a fresh checkout at commit `2550ad2` using a newly created Python 3.12 environment and locked dependencies passed 221 tests with one skip in 26.88 seconds, without the supplied key file. Both actual-process crash cases and the portable-recording regression are included. Ruff lint, formatting of 19 source/test files, and dependency consistency checks passed in both environments.
- The actual-key audit passed for all Git objects, 44 project files, 10 persisted JSON files, six Parquet files (including decompressed `_source_row` content), and the full/recent logs. The provided `key.txt` remains ignored and untracked. The audit counts describe its scope at execution time, not a guarantee about future edits.
- The skipped case is Windows symbolic-link privilege availability; it is not silently treated as a successful symlink-boundary check. The configured Linux CI job provides a platform where that check can run, but remote CI execution has not been asserted.

The two crash tests terminate a real subprocess immediately before publication and immediately after commit. Recovery preserves the prior snapshot in the first case and the newly committed snapshot in the second, while releasing the OS writer lock.
