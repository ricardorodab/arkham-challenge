# Engineering decisions — Parts 1–3

This document describes the implemented connector, analytical model and Django API. The web application and Azure hosting remain future work.

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

An explicit start is accepted only with full mode. Bounded full extraction replaces its interval while retaining observations outside it; unbounded full extraction replaces all stored history. If a refresh would delete observations, a second extraction must confirm identical logical source content. In full and recent modes, a source start later than stored coverage or an end before the stored watermark fails before data requests, rather than silently shrinking history.

## 6. Treat source totals as diagnostic metadata

**Choice:** explicit complete-key sort, offsets advanced by received row count, and termination on an empty page with repeated/non-progress guards. Record declared versus observed counts.

**Rejected:** terminating solely when offset reaches `response.total`, or failing every run when that total disagrees with observed records.

**Reason:** preliminary source inspection observed an inconsistent facility total. A short page or advertised count is insufficient proof of completion. Pagination checks and coverage checks provide stronger evidence, while source consistency remains a documented limitation.

The completed full-history run confirmed this distinction: facility declared 714,657 rows, but 438,119 facility observations were actually received and published. Generator contained 714,657 rows. This is a reproducible observed API-metadata inconsistency, not an explanation of differences between the analytical grains.

A missing metadata-advertised source boundary prevents publication as potentially truncated. Other missing requested/calendar dates are reported in quality metadata rather than fabricated. These checks do not prove that every historically expected generator was supplied by EIA.

## 7. Fail on broken structure; preserve suspicious measurements

**Choice:** missing identity, invalid dates, missing required fields, incompatible units, malformed/non-finite numbers, lossy conversion, and conflicting natural keys fail publication. Nullable measures and semantic outliers remain present with quality flags. Identical duplicate rows may be collapsed.

**Rejected:** coercing missing measurements to zero, discarding generator orphans, or clipping values to expected bounds.

**Reason:** structural failures undermine identity and interpretation. An unusual measurement may be legitimate, and deleting it would destroy evidence. Part 2 explains observed cross-grain differences in `FINDINGS.md`; transformations must not manufacture agreement.

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

## 14. Model source grains separately and preserve missing parents

**Choice:** separate national, facility and generator facts, with date and observed-identity dimensions. A generator key includes its facility. Observation-time names remain in the facts; the facility dimension carries the latest observed descriptive name. Reconciliation uses full outer coverage rather than inner joins.

**Reason:** the three routes are independently published observations. A generator can exist while its facility-day observation is absent. Its FK points to the identity dimension, not to a facility fact, so that absence survives as evidence. No missing row becomes zero, and no generator is discarded to satisfy a relationship. The dimensional name is not a claim of complete temporal ownership/name history.

## 15. Derive US-08 from national operands with explicit decimal semantics

**Choice:** use national outage divided by national capacity for the same day. Python `Decimal` uses precision 28 and `ROUND_HALF_UP`; share is stored at 12 decimal places, percentage at nine. Calculate each from the unrounded ratio. Nonpositive/null denominator or null outage yields a null result and quality flag. Preserve all source values, including implausible ratios.

**Rejected:** averaging child percentages, dividing through binary floating point for the canonical metric, silently substituting child totals, or clipping to 0–100%.

**Reason:** changing numerator, denominator or rounding order changes the metric. DuckDB decimal division returns floating point, so canonical ratio calculation occurs in Python. SQL reports label weighted published percentages as diagnostics. Absolute percentages use percent; a difference between percentages uses percentage points. See the catalog's versioned metric definition and the exact arithmetic rounding tests.

## 16. Reconcile exact MW before interpreting percentage differences

**Choice:** retain parent values, child sums, child/null counts, signed and absolute gaps, and coverage status. Null child measures make the relevant sum undefined rather than allowing SQL `SUM` to conceal them. Classify gaps using 0.01 MW and 0.005 percentage-point tolerances, while retaining the actual differences.

**Reason:** a weighted average of independently rounded percentages need not equal a rounded national ratio. The real 30-day example has zero MW gaps; inventing a numerical discrepancy would misrepresent the data. River Bend demonstrates that all grains can agree while sharing a coverage omission. Reconciliation and entity-coverage checks answer different questions.

## 17. Use one sorted file per dataset at this measured volume

**Choice:** ZSTD Parquet sorted by natural key, with date first for daily facts and reconciliation. One file per dataset per model; row groups target 122,880 rows. The raw three-route snapshot is about 46.4 MB compressed. No year/month directory partitions yet.

**Reason:** expected reads filter by date and may scan the whole history. Many tiny date partitions would add file-opening and metadata overhead without a demonstrated benefit. At larger volume, benchmark date pruning, memory, latency and file sizes before choosing annual/monthly partitions; retain useful file sizes, avoid facility-level partition explosion, and compact small outputs. `docs/DATA_MODEL.md` describes the trade-offs and migration triggers. This is a deliberate partitioning strategy, not an unmeasured scalability claim.

## 18. Publish derived and raw generations together and retain versioned contracts

**Choice:** build and verify all model files before one SQLite transaction advances both pointers and completes both jobs. A standalone offline rebuild advances the model only if its raw source is still current. Each model records source hashes/counts, schema/metric versions, file and logical hashes, and quality rules. Retain immutable historical artifacts.

**Reason:** serving a new source through an old model would create contradictory results. Three actual-process crash positions now cover raw-file rename, model-file rename, and post-commit recovery. Unreferenced files may remain after a failure; they are never promoted by directory discovery. There is no automatic retention or off-machine backup in this part.

The first local model version 1 used incorrect catalog unit labels for absolute percentages. Its numerical data was correct. Rather than rewriting published evidence, the original contract was frozen in `contracts/model-v1.json`; version 2 corrects labels. Historical v1 reads use that frozen profile and expose a warning. All ten logical data hashes matched between versions; the metric formula remains version 1.

## 19. Expose backend-ready metadata without claiming a user-SQL boundary

**Choice:** logical dataset IDs, explicit contracts/metric definitions and a context-managed DuckDB adapter for trusted backend code. Resolve one model per operation; reject disallowed datasets before reading their Parquet; verify selected file hashes before registering views. Only national/fleet metadata is available to the Viewer policy. Reports run fixed packaged SELECT assets with bound dates.

**Reason:** Django handlers can consume stable modeled data without reimplementing metrics. A caller able to execute arbitrary SQL on a local DuckDB connection can bypass a view-only convention; therefore the adapter remains trusted-code-only. Part 3 adds separate authenticated capabilities and an isolated user-query path; it never exposes this native connection to submitted SQL.

## 20. Automate anomaly detection without silently repairing history

**Choice:** deterministic review events for nonpositive capacity, zero-capacity/nonzero percentage, observed-span entity gaps, consecutive capacity changes and reconciliation issues. Check rules during every model build; structural violations fail publication, semantic warnings are preserved.

**Reason:** the three documented historical anomalies now have concrete stored events and reproducible SQL. Coverage events identify missing observations between observed dates; they do not infer a plant's entire operating lifetime. A capacity change of at least 100 MW and 10% is a review threshold, not proof of an EIA mistake. Thresholds, units and event identity are part of the versioned model contract.

## 21. Use Django sessions and a single server-owned product persona

**Choice:** Django 5.2 LTS, server-side sessions, CSRF on all POSTs, and one validated Viewer/Analyst/Admin record per user. Seed accounts use environment-supplied passwords; the explicit local setup generates private random credentials. Product Admin has no Django staff/superuser privilege.

**Rejected:** trusting a persona header/body field, granting implicit product access to staff, or shipping a shared hardcoded demo password.

**Reason:** this fits the user's Django experience and supplies established password/session handling. The policy reloads the active user, persona, authentication hash and persisted unexpired session before access and delivery. Revocation during execution cannot be hidden by cached request identity or cached results. The CLI/local filesystem is an operator boundary, not a remote product capability.

## 22. Keep user SQL deliberately small and inspect every reference

**Choice:** one selected dataset; SELECT projections, predicates, arithmetic, ordering, grouping and explicit safe scalar/aggregate functions. Reject commands, writes, table functions, arbitrary functions, JOIN/CTE/subquery/UNION/window syntax, database/schema qualifiers and unknown columns. Compile using SQLGlot's DuckDB dialect, then execute only regenerated SQL.

**Rejected:** first-keyword checks, regex table extraction, trusting a client-selected dataset while ignoring other references, or supporting SQL syntax without defining its full authorization behavior.

**Reason:** the minimum is useful and defensible within this part. The compiler walks every table node, preserving references even when a query shape is unsupported; the web service authorizes each discovered logical name before analytical I/O. Unsupported syntax cannot smuggle detail through a permitted national dataset. Broader SQL is an optional future language expansion requiring new scope/column/permission tests.

Compilation itself runs in a disposable Python process with bounded input/output, memory and a two-second subdeadline, rather than parsing untrusted text recursively inside Django. Input tokens, depth, AST size and generated SQL are also bounded. No in-process compilation or native-engine fallback exists.

## 23. Use a real WASM capability boundary for the local query engine

**Choice:** a disposable DuckDB-WASM process receives one checksummed Parquet buffer over stdin through a custom BUFFER-only runtime. Bootstrap preloads one pinned offline Parquet extension, then disables external access/autoload and locks configuration. Application secrets, model host paths and other datasets are absent. Windows Job Objects apply hard process memory/CPU/lifetime limits; WASM/V8 and output/deadline limits add independent bounds.

**Rejected:** treating DuckDB settings or SQLGlot as a sandbox, same-user native DuckDB with full host capabilities, or declaring an unavailable Docker/WSL environment tested.

**Reason:** the actual Windows machine has neither Docker nor installed WSL. DuckDB's primary security guidance explicitly lists WASM as a sandbox option. The custom runtime was necessary because the npm package's normal Node runtime can access the host filesystem. Engine-level tests independently deny host CSV/Parquet, HTTP, COPY and ATTACH, without relying on AST rejection. [DuckDB security guidance](https://www.duckdb.org/docs/2.0/operations_manual/securing_duckdb/overview).

Native DuckDB 2.0 remains trusted ingestion/modeling code; the pinned npm runtime embeds DuckDB 1.5.4. Interoperable Parquet and exact decimal/date behavior are tested. This costs another runtime and startup/whole-file-transfer overhead; a per-query 64 MiB input limit is explicit. POSIX allocation limits are implemented but not asserted live-verified here. The deployment platform must repeat acceptance before hosting.

## 24. Pin navigation and private cached results to immutable generations

**Choice:** signed, expiring cursors bind user/session/persona, request parameters and one model. Preview uses full-primary-key keysets. SQL returns at most 1,000 retained rows and pages within that cap. A 60-second cache includes the owner/authentication binding, canonical query, selected model and current publication generation; only eight entries per user are retained.

**Rejected:** a shared SQL-string-only cache, unsigned offsets, applying permissions only when a result is first generated, or silently moving a continuation to new data.

**Reason:** identical text does not imply identical authorization or source version. Permission checks precede lookup and repeat before delivery. A new publication changes the cache generation even if cleanup fails, while an explicitly pinned cursor can still read immutable history. A cache hit remains subject to admission and reauthorization.

## 25. Refresh asynchronously through a durable, separately invoked worker

**Choice:** Admin POST creates a database job and returns 202/status URL. A separate worker consumes it, owns the EIA environment key and calls the existing joint-publication pipeline. DB constraints enforce one queued/running refresh; owner-scoped idempotency keys deduplicate retries. Job status is visible only to its active Admin owner.

**Rejected:** doing a multi-minute download inside an HTTP request, background threads that vanish with a web process, or exposing a generic shell/task endpoint.

**Reason:** the real full-history rebuild takes minutes while HTTP polling stays responsive. A distinct OS worker-control lock distinguishes an active worker from an abandoned job. After process death, the next worker records interrupted work. Publication is authoritative in the data catalog: a crash after its commit but before the separate job completion may have published, so the job message explicitly requests publication review. It never falsely promises an unchanged source. No exactly-once external-source transaction is claimed.

## 26. Bound admission and expose an inspectable API contract

**Choice:** process-shared OS locks permit two admitted operations, one per account and eight waiting. Queue wait is capped at five seconds; compiler/startup/input/execution share eight admitted seconds. Row/output limits apply independently. Errors use Problem Details, static safe messages and server-generated request IDs; logs omit SQL, bodies, cookies, credentials and concrete filter values. OpenAPI 3.1 and health endpoints document the contract.

**Rejected:** process-local counters, a LIMIT-only cost policy, unbounded queues, or passing engine exceptions and physical paths to clients.

**Reason:** multiple HTTP processes must share capacity, and aggregate/sort cost is independent of returned row count. OS locks release on process death. Public liveness reveals no data; readiness requires Admin and checks local model/runtime availability. The shipped Waitress launcher binds loopback with explicit HTTP/body/connection/thread limits. It is a local backend, not an Azure deployment or an availability guarantee.
