# Engineering notes — Parts 1–3

## Attribution and scope

The connector, analytical model, Django backend, tests, and supporting documents were authored with AI assistance in this Codex chat, including delegated implementation and independent review. The user supplied the challenge and API credential, selected Django/product constraints, and requested the implementation. No code should be attributed to manual user authorship without evidence.

This delivery includes Parts 1–3. Part 4's web UI and Azure deployment remain future work. The backend uses authenticated personas and an isolated, deliberately bounded SELECT subset. The local CLI remains an operator tool.

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
- Semantic outliers are preserved; Part 2 explains three real cases, but review events do not prove a specific upstream root cause.
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

## Part 2 corrections and verification

The AI generated the model builder, integration, reports and tests. Independent agent review then compared raw observations with every modeled fact and independently reaggregated reconciliation values. Real rows and primary EIA/NRC references were used to investigate findings; a plausible source explanation was not promoted to a proven root cause.

Concrete errors caught during this implementation:

1. **DuckDB result lifetime:** using the relation API to register a Parquet view while a previous query result was pending raised a DuckDB 2.0 resource-use error. The implementation now creates views through explicit SQL with safely escaped, server-owned paths and closes connections deterministically. No user-provided path or SQL is interpolated. Real full-history model construction, adapter tests and the packaged report verified this correction.
2. **Unit metadata after publication:** catalog metadata incorrectly used “percentage points” for absolute percentages. Numerical Parquet values were correct. Review caught the semantic mismatch; version 2 fixes labels, while a packaged frozen v1 contract preserves the first immutable model and emits a legacy warning. Comparing all ten logical hashes established that this metadata correction did not change observations or numerical results. Contract-tampering and historical-profile tests verify the distinction.
3. **A report test assumed a fixed old query count:** adding the eighteenth query that proves the three real stored DQ events initially left assertions expecting seventeen. The full suite caught this. Both asset and CLI assertions were updated to require the new evidence query and its behavior on synthetic data. This test maintenance error did not alter the real findings.
4. **Elapsed-time reporting after adding model construction:** the success log reused the raw manifest's pre-model extraction duration. Review of the real refresh timestamps caught the misleading total. The success event now times through joint publication. Historical raw manifests remain unchanged; their duration measures raw preparation, and model history records separate build timestamps.

Part 2 verification includes exact Decimal edge cases, duplicate/invalid-key rejection, null and missing-child coverage, generator orphans, all PK/FKs, semantic metadata and file tampering, wrong/stale source binding, Viewer policy rejection before file reads, standalone and joint publication rollback, three actual-process crash boundaries, offline CLI/report behavior, packaged SQL availability, and independent comparisons against the complete real source. Acceptance commands, observed results and limitations are recorded in `PART2_ACCEPTANCE.md`.

The integration live refresh published raw snapshot `1e6f80a89cb4455d949d541a6fe68e2a` with model `9811a42ac9a34b4581d00a9bbd5d5d9b`. It observed zero added/changed/deleted rows and preserved all ten analytical logical hashes. `FINDINGS.md` deliberately retains its earlier model pin so subsequent refreshes cannot silently change the evidence.

The Part 2 full suite passed on Python 3.14.2 and 3.12.14 with 302 passes and one Windows privilege-dependent skip. A new checkout at `f3b7cd6`, fresh Python 3.12 environment and locked dependencies reproduced all 302 passes without a key or copied data. The installed-wheel report also matched the canonical report outside the source tree. Complete current-model verification and the supplied-key persistence audit passed; `PART2_ACCEPTANCE.md` records their scope, commands and observed results.

## Part 3: concrete generated-code mistakes caught by review

1. **Cached request identity survived server-side logout.** The first recheck reloaded the user/persona and password hash but trusted the request's already-loaded session fields. Deleting the persisted session while a query ran could still allow its result and cache write. Independent review reproduced this; policy now checks the actual unexpired Session row and its user/backend/auth hash. Tests delete/expire/corrupt that row or perform a real same-session logout during execution, then require 401 and no cache entry.
2. **Malformed selectors silently selected current data.** Falsy JSON values such as `false`, `0`, `[]` or an empty model ID reached a helper that interpreted them as “not supplied.” Explicit API validation now accepts only null/absence or a 32-character hexadecimal model ID before model resolution. Tests prove invalid values cause 400 without analytical I/O. Lone-surrogate Unicode credentials also initially caused an encoding 500; UTF-8 validation now rejects them before throttling/authentication.
3. **A process limit initially killed normal compilation.** V8 optimizing WASM compilation exceeded the intended 512 MiB Windows Job limit. The fix retained the limit and selected one baseline compilation task with no optimizing tier-up; actual full generator counts and previews then passed. A separate Windows Python-venv redirector attempted a second process inside the one-process Job. The compiler now launches the trusted base interpreter with the venv launcher environment, still in isolated Python mode. Resource tests verify hard memory denial, deadline kill/reap and subsequent recovery.
4. **Parser work had remained inside Django.** Bounded SQL text alone did not fulfill the plan to move potentially expensive parsing out of the web process. Compilation now runs in a separate time/memory-limited Python child; every returned reference is authorized before data access. The compiler and WASM share the same admitted eight-second deadline. Tests prove there is no parent-process parser fallback and that an exhausted budget never starts the engine.

Verification went beyond happy-path responses: direct engine probes bypassed the SQL policy to test the actual file/network boundary; private canary files and a localhost HTTP receiver remained inaccessible. SQL injection/unsupported AST tests, session/role revocation, cache and cursor isolation, process-shared admission and real killed-worker recovery test distinct boundaries. OpenAPI was checked by a standards validator; a real Waitress HTTP session then exercised all three personas and a genuine queued EIA refresh. `PART3_ACCEPTANCE.md` records observed counts, versions, timings and limitations. Linux resource behavior is not inferred from a Windows pass.
