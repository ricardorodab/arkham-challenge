# Part 1 engineering decisions

This document describes the connector implemented for Part 1. Decisions about the future web app, arbitrary user SQL, authorization, and Azure hosting remain future work; they are not implemented by the connector.

## 1. Natural keys follow the actual source grains

**Choice:** national `(period)`, facility `(period, facility)`, generator `(period, facility, generator)`. Preserve identifiers as text and names as descriptive source attributes.

**Rejected:** treating a generator identifier or facility name as globally unique.

**Reason:** a generator ID is scoped to a facility, and names can change. These keys support deterministic duplicate detection, replacement, and change accounting.

## 2. Store source observations as Parquet using DuckDB

**Choice:** typed Parquet, with DuckDB performing local reads/writes. Preserve a sanitized original source row alongside typed values.

**Rejected:** CSV-only persistence or adding a server database for the analytical extracts.

**Reason:** Parquet is required by the brief, and DuckDB handles it directly. Keeping the original values makes conversion and anomaly investigation reproducible. Measures use `DECIMAL(24,9)` with loss detection rather than implicit rounding.

## 3. Keep one local publication authority

**Choice:** SQLite operational state owns the current snapshot, successful watermarks, and run status. Publish all three routes together after writing immutable files. The manifest records evidence, not a second current pointer.

**Rejected:** independent `current.json` and database updates, or overwriting the current Parquet files in place.

**Reason:** one database transaction prevents mixed route generations and contradictory success/current state. Filesystem and SQLite cannot share one transaction: a failed commit may leave an unreferenced directory, but readers will not mistake it for published data.

The full deployed product is planned to move this authority to PostgreSQL. Part 1 remains runnable locally without Azure or a database service; this is a scoped implementation decision, not a claim that SQLite is the final hosted architecture.

## 4. Prefer coherent publication to partial availability

**Choice:** any failed route or invalid required data prevents the complete run from becoming current.

**Rejected:** publishing successful routes one at a time.

**Reason:** downstream users should never unknowingly compare national data from one extraction with generator data from another. The source itself is live and not transactionally frozen; record extraction times and per-route coverage rather than claiming simultaneous EIA observations.

## 5. Define “kept current” explicitly

**Choice:** full extraction uses the available source history; recent extraction rereads each route from its last successful date minus 29 days through its own current metadata end. Replace that validated interval, including source deletions, and retain older observations outside it.

**Rejected:** appending only dates newer than the watermark, or assuming a 30-day overlap finds every historical revision.

**Reason:** EIA can revise older observations and has no general update timestamp. The overlap catches recent changes; periodic full refresh is still needed. Scheduling belongs to later operational work. Display actual source coverage and extraction times instead of promising that today's data must already exist.

An explicit start is accepted only with full mode. Bounded full extraction replaces its interval while retaining observations outside it; unbounded full extraction replaces all stored history. If a refresh would delete observations, a second extraction must confirm identical logical source content. Recent metadata regression behind the stored watermark fails instead of silently moving coverage backwards.

## 6. Treat source totals as diagnostic metadata

**Choice:** explicit complete-key sort, offsets advanced by received row count, and termination on an empty page with repeated/non-progress guards. Record declared versus observed counts.

**Rejected:** terminating solely when offset reaches `response.total`, or failing every run when that total disagrees with observed records.

**Reason:** preliminary source inspection observed an inconsistent facility total. A short page or advertised count is insufficient proof of completion. Pagination checks and coverage checks provide stronger evidence, while source consistency remains a documented limitation.

The completed full-history run confirmed this distinction: facility declared 714,657 rows, but 438,119 facility observations were actually received and published. Generator contained 714,657 rows. This is a reproducible observed API-metadata inconsistency, not an explanation of differences between the analytical grains.

A missing metadata-advertised source boundary prevents publication as potentially truncated. Other missing requested/calendar dates are reported in quality metadata rather than fabricated. These checks do not prove that every historically expected generator was supplied by EIA.

## 7. Fail on broken structure; preserve suspicious measurements

**Choice:** missing identity, invalid dates, missing required fields, incompatible units, malformed/non-finite numbers, lossy conversion, and conflicting natural keys fail publication. Nullable measures and semantic outliers remain present with quality flags. Identical duplicate rows may be collapsed.

**Rejected:** coercing missing measurements to zero, discarding generator orphans, or clipping values to expected bounds.

**Reason:** structural failures undermine identity and interpretation. An unusual measurement may be legitimate, and deleting it would destroy evidence. Part 2 will explain observed cross-grain differences; Part 1 must not manufacture agreement.

## 8. Handle additive schema changes without silent data loss

**Choice:** preserve new source fields in sanitized source JSON and report them in run metadata. Keep a stable typed contract for required fields. Reject incompatible required-field evolution.

**Rejected:** dropping every unrecognized field or changing typed schema implicitly from the first response.

**Reason:** additive source information should remain available for review. Breaking changes need an explicit code/schema decision rather than a silently corrupted extract.

## 9. Conservative concurrency and bounded retry

**Choice:** one route worker by default; optional workers are limited to three and share a global rate gate. Retry transient network/5xx/429 failures with bounded backoff; honor `Retry-After` and stop when the requested wait exceeds the allowed execution policy.

**Rejected:** unbounded parallelism, immediate retries, or sleeping indefinitely.

**Reason:** source limits depend on key, IP, route, and demand. More threads must not multiply the permitted global request rate. Explicit failures let a later invocation resume without obscuring the previous valid snapshot.

## 10. Use actual process locking and recoverable run records

**Choice:** hold an OS writer lock for ingestion, keep durable run states, and reconcile interrupted work after acquiring that lock.

**Rejected:** treating the presence of a `.lock` file as proof that a writer is alive.

**Reason:** the operating system releases a process lock after a crash. A leftover file need not block future work, and another live writer must never be displaced based only on timestamps.

## 11. Secrets are excluded from provenance

**Choice:** read the EIA key only from `EIA_API_KEY`; scrub nested request echoes, URLs, exceptions, fixtures, and recordings before persistence. Ignore local credential files and generated data before the first commit.

**Rejected:** committing the provided key file, automatically loading it in application code, or retaining raw full responses that echo the key.

**Reason:** environment configuration satisfies the brief, and a sanitized request still contains enough information to reproduce it with a separately supplied key.

## 12. Verify behavior with offline fixtures and separate live evidence

**Choice:** deterministic offline tests plus explicitly recorded/sanitized source fixtures, then a separate live run and repeat/verify acceptance sequence.

**Rejected:** making CI depend on EIA uptime, treating invented rows as findings, or considering a single successful run proof of idempotency.

**Reason:** failure behavior must be reproducible. Tests, remote extraction, stored-data integrity, and rerun safety prove different things and require separate evidence.

Genuine sanitized metadata and first-page responses for all three routes, plus an independently captured HTTP 403 credential failure, are retained under `tests/fixtures/recorded/`. Their provenance identifies partial capture scope, timestamps and hashes. Synthetic fixtures and synthetic terminal pages remain explicitly labeled. A successful full backfill and a real recent rerun produced the same three logical hashes; the final evidence matrix records their run IDs and counts.

## 13. Distinguish validation evidence from committed success

**Choice:** per-run `quality.json` initially records `status: validated`; the SQLite transaction alone determines successful publication. Failed/interrupted work receives the corresponding failure quality state when diagnostics can be persisted.

**Rejected:** labeling a pre-commit quality file as a successful publication.

**Reason:** a process can die between writing quality evidence and committing state. Two subprocess-death tests cover a crash before the commit and a crash after the commit; the database pointer and history must retain the correct state in both cases.
