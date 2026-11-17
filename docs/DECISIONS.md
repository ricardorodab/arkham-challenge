# Engineering decisions and trade-offs

This record explains the current design and its main limits. The application runs locally or on one persistent Azure Linux VM. SQLite remains the publication and application-state store; PostgreSQL and horizontal scaling are future options, not deployed features. Start with the [project tour](PROJECT_TOUR.md); detailed contracts and commands stay in the linked guides.

The challenge's seven required choices map to: natural keys (§1), kept-current behavior (§3), missing/mismatching parents (§5), asynchronous refresh (§11), supported/rejected SQL (§9), discovery and authorization of every named table (§9), and coherent publication (§4).

## 1. Identify observations by their source grain

**Decision:** national keys are `(period)`, facility keys `(period, facility)`, and generator keys `(period, facility, generator)`. A grain describes what one row represents. Keep identifiers as text; names describe identities but do not define them.

**Why and trade-off:** generator IDs are unique only within a facility, and names can change. Natural keys support duplicate detection and interval replacement without treating identifiers as quantities or plant names as permanent identities.

## 2. Preserve source evidence and separate errors from warnings

**Decision:** store typed Parquet with sanitized source JSON and row fingerprints. Measures use `DECIMAL(24,9)`. Scrub credentials from nested request echoes, URLs, errors and recordings before persistence. Missing identity/required fields, invalid dates/units, malformed numbers, lossy conversion and conflicting keys block publication. Identical duplicates may collapse; null measures and representable outliers remain with quality flags.

**Why and trade-off:** CSV-only storage loses useful types; silently filling, clipping or dropping unusual values changes the evidence. Additive source fields remain in sanitized JSON and run metadata, while incompatible required-field changes fail explicitly. An empty flag list means no configured row warning, not proof that the source is complete or correct. See the [validation contract](DATA_PIPELINE_AND_MODEL.md).

## 3. Refresh a correction window and verify pagination

**Decision:** recent mode rereads each route from its successful watermark minus 29 days through its latest metadata end: a 30-day overlap. Bounded full mode replaces its requested interval; unbounded full mode replaces all stored history. An explicit start requires full mode. Proposed deletions need a second matching extraction; source boundaries that would shrink stored coverage fail before data requests.

Fetch in complete-key order, advance offsets by rows received, and continue to an empty terminal page with repeat/non-progress guards. Provider totals are diagnostic, not the stopping rule. Missing advertised boundaries fail validation; other calendar gaps remain reported evidence. Use one route worker by default, at most three, sharing a rate gate and bounded backoff honoring `Retry-After`.

**Why and trade-off:** append-only downloads miss corrections; trusting a reported total can truncate extraction. The overlap can still miss older revisions, so broader/full refreshes remain necessary. Data refresh is not automatically scheduled, and EIA's live pages are not a transactional snapshot. Historical provider-count discrepancies are documented in [findings](FINDINGS.md).

## 4. Publish matching source and model versions together

**Decision:** close, synchronize and verify immutable candidate files before one SQLite catalog transaction advances the raw/model references and completes ingestion/build records. A standalone model rebuild advances only if its raw source is still current. The catalog is authoritative; manifests and directory names are evidence, not independent current pointers.

**Why and trade-off:** per-route publication or in-place overwrites could mix generations. Filesystem writes and SQLite cannot share one transaction, so failures may leave unreferenced candidates. Recovery runs under the OS writer lock and updates abandoned run states; file presence alone never proves a live writer or successful publication. A pre-commit `validated` report is not committed success. Automatic orphan cleanup and reference-aware retention are still absent.

Application refresh-job completion uses a separate database transaction. An interrupted job may already have published data: inspect the catalog before retrying. See [publication and recovery](DATA_PIPELINE_AND_MODEL.md).

## 5. Keep independent facts and reconcile without repairing them

**Decision:** national, facility and generator observations remain separate daily facts. Dimensions describe observed dates and identities; facts retain observation-time names. Generator keys include facility, and generator facts reference identity dimensions rather than requiring a matching facility-day fact. Full-outer reconciliation preserves unmatched parents and children.

**Why and trade-off:** inner joins, zero-filling and rescaling child totals would hide disagreement or missing observations. Null-aware child sums retain incomplete measures as undefined. Store actual signed/absolute gaps and classify them with tolerances of **0.01 MW** and **0.005 percentage points**. Aggregate agreement does not establish complete entity coverage or physical cause.

Model builds also record deterministic quality events for nonpositive capacity, zero-capacity/nonzero percentage, observed-span gaps, capacity changes and reconciliation issues. The capacity-change threshold (at least 100 MW and 10%) prompts review; it does not prove an EIA error. See [findings](FINDINGS.md) for concrete examples and qualifications.

## 6. Define the fleet metric and version its meaning

**Decision:** the fleet outage share is same-day national outage MW divided by national capacity MW. Python `Decimal` uses precision 28 and `ROUND_HALF_UP`, storing share to 12 places and percentage to nine independently from the unrounded ratio. Null outage or null/nonpositive capacity yields an undefined metric with a flag; representable outliers are not clipped.

**Why and trade-off:** unweighted child percentages and binary floating-point division change the calculation. Derived overflow or failed quantization rejects the candidate with `invalid_model`, retaining the previous publication; valid source operands do not guarantee a derived value will fit.

Model v2 corrected percentage unit labels without changing numerical data. The frozen [v1 contract](../src/outage_explorer/analytics/contracts/model-v1.json) keeps older artifacts readable with a warning; the metric formula remains version 1. Absolute percentages and percentage-point differences remain distinct.

## 7. Use Parquet layout and readers suited to each task

**Decision:** one ZSTD Parquet file per dataset/model, sorted by natural key with date first for daily data; row groups target 122,880 rows. Native DuckDB handles trusted transforms, packaged reports and structured Explore requests. Reports may use joins and fixed SQL with bound dates; this does not expand user SQL permissions.

Explore applies typed filters, literal search and sort-plus-primary-key seek pagination across the selected dataset, returning only a bounded page. It uses a separate isolated native reader, not the WASM whole-file buffer.

**Why and trade-off:** many small date partitions add overhead without demonstrated benefit at this volume. Seek avoids deep OFFSET growth but does not create an index; substring search and arbitrary sorting may scan input. The [three-million-row benchmark](BROWSE_PERFORMANCE.md) is scoped synthetic evidence, not a concurrent-load or latency guarantee. Partitioning and incremental model materialization require further measurement; every refresh currently rebuilds the full model.

## 8. Keep identity and account policy on the server

**Decision:** Django sessions, CSRF and a persisted product persona authorize requests. Viewer accesses national/fleet datasets; Analyst accesses all ten; Admin additionally manages accounts and refresh. Product Admin does not imply Django staff/superuser or VM access. Persisted user/session/role checks repeat before result delivery and cache use.

**Why and trade-off:** client role fields, staff flags and cached identity alone cannot establish current permission. Admin user changes use version checks to prevent lost updates; role/activity changes and password resets retire sessions. Random passwords are returned once and stored as hashes. Existing account edits/deletions survive initialization; bootstrap credentials do not track later passwords. See [account and session rules](API_CONTRACT.md).

## 9. Restrict user SQL and isolate its execution

**Decision:** accept a single-dataset, read-only SELECT subset: projections, predicates, arithmetic, ordering, grouping and allowlisted functions. Reject writes, table functions, arbitrary functions, joins, CTEs, subqueries, unions, window syntax, qualifiers and unknown columns. Discover every named table and authorize it before analytical I/O, including references in unsupported shapes; execute regenerated SQL only.

A bounded disposable Python compiler precedes a disposable Node/DuckDB-WASM worker. The latter receives only selected checksummed Parquet bytes through a BUFFER-only runtime, with offline extension loading followed by locked external-access settings. SQLGlot parsing and engine settings alone are not the sandbox; there is no native-engine fallback for submitted SQL.

**Why and trade-off:** the original Windows environment lacked Docker/WSL, explaining the initial WASM choice; Linux Docker acceptance and Azure hosting are now verified. Native DuckDB 2.0 and WASM DuckDB 1.5.4 have different trust roles. Isolation costs child startup and whole-file transfer, including a **64 MiB selected-Parquet input ceiling for user SQL**. OS and engine limits supplement capability restrictions; the Python compiler is resource-bounded, not filesystem-jailed. See [query security](QUERY_SECURITY.md) for platform-specific memory/CPU limits and engine denial tests.

## 10. Bound requests, caches and continuation

**Decision:** OS-backed admission allows one pending/executing analytical request per account, two execution slots and eight waiting slots. Waiting is capped at five seconds. Startup, preparation, compilation and execution share eight admitted seconds; the compiler gets at most two. SQL retains at most 1,000 rows with 2 MiB output.

Signed cursors expire after 15 minutes and bind identity/session, effective request and immutable publication; they are not encrypted. SQL caches last 60 seconds, at most eight per user, and bind session/role/query/limits/model/current generation. Cache hits still undergo admission, compilation and authorization.

**Why and trade-off:** LIMIT does not bound scan or aggregate cost. Shared locks coordinate processes and release after crashes; process-local counters do not. Unscoped caches and unsigned offsets risk cross-user or mixed-version results. Errors use static Problem Details and request IDs, excluding raw SQL, credentials and engine paths. [OpenAPI and API contracts](API_CONTRACT.md) define exact behavior.

## 11. Run refresh outside HTTP and show measured progress

**Decision:** Admin POST durably queues a job and returns `202` plus a status URL. A separate worker holds its control lock, rechecks the owner's active Admin permission, and runs ingestion. A database constraint permits one queued/running job globally; owner-scoped idempotency keys deduplicate retries. Job history is owner-only.

Persist actual stages, route/page/row counts, timestamps and outcomes. Only the fixed model-export stage has a truthful completion percentage; there is no overall ingestion percentage or heartbeat claim. Unknown POST outcomes reuse the same key.

**Why and trade-off:** long downloads cannot belong inside HTTP requests or disposable web threads. Worker restart marks abandoned running jobs interrupted, but cannot undo an already committed publication or guarantee exactly-once extraction. Progress remains inspectable after reload/failure.

**Open issue:** if a competing job finishes after an enqueue constraint failure but before its error-handler checks, the request can return `500` instead of the intended `409 refresh_busy`. The one-active-job constraint still holds. This classification race remains unfixed.

## 12. Keep the browser useful without moving trust into it

**Decision:** Django templates and native browser modules call the API; normal installation uses packaged assets. CodeMirror provides SQL highlighting/completion, selectable schema fields and three query starters per dataset. English/Spanish labels leave canonical SQL/data identifiers unchanged.

Overview, Explore and SQL share one pinned model; **Use latest** explicitly adopts a new publication. Local loaded-page filtering stays separate from an applied database query. Analyst/Admin can save named tables/charts and choose sharing audiences; owner mutations and each reader's dataset permissions remain authoritative. Saved definitions do not grant data access or persist result rows.

**Why and trade-off:** a separate frontend platform or browser analytics engine would duplicate responsibilities. Exact typed values remain text in tables/CSV, with formula guards; charts preserve zero and gaps and offer a semantic table. Responsive layouts, keyboard controls and accessibility scans complement visual checks, not certify every assistive technology.

At most ten recent query texts/statuses stay in session-scoped `sessionStorage`, without result rows. Login/logout, revocation and stale-response guards retire private state; uncertain network outcomes keep a retry shield. These controls cannot erase information already observed. See the [browser guide](BROWSER_WORKSPACE.md).

## 13. Deploy one persistent VM with managed secrets

**Decision:** Docker runs separate web/refresh processes behind Caddy HTTPS on one Azure Linux VM. Waitress binds inside its container; its host port stays on loopback. SQLite and immutable files remain on persistent host mounts. PostgreSQL/shared coordination and horizontal replicas require a broader design.

Local setup retains private generated JSON credentials and process-environment inputs. Production instead fetches five application secrets from private Key Vault using a separate VM managed identity. Each process receives only its read-only temporary secret files; strict file-only mode rejects secret-valued environment variables. Container access to identity endpoints stays blocked, the Django key is pinned, and logs redact the exact loaded EIA key. Moving a key does not rotate it.

**Why and trade-off:** Key Vault centralizes credential access, while VM/root/Docker operators remain trusted and cold starts depend on private DNS/network/vault availability. Protected `main` requires PRs and checks, including for admins. GitHub's OIDC identity deploys the accepted immutable image and verifies its revision over HTTPS; root deployment authority remains privileged. Independent PR approval awaits another reviewer. See [deployment](../deploy/README.md) and [security policy](../SECURITY.md).

## 14. Keep verification and recovery claims scoped

**Decision:** offline synthetic and sanitized recorded fixtures make failures reproducible; live extraction, repeat-run hashes, independent arithmetic and actual process/container/browser tests provide separate evidence. CI covers Windows/Linux, SQL engines, secret handling and deployment; [README verification](../README.md#verification-status) links the verified baseline. Partial first-page recordings are not a complete source capture, and synthetic data is not a real-world finding.

**Why and trade-off:** reproducibility cannot depend on EIA uptime or one successful invocation. Real crashes test specific publication boundaries, not every disk/power failure. Encrypted off-host backup/restore tools are implemented and tested, but their storage and timers remain inactive in the current deployment. Immutable files and local migration copies do not replace an operational off-machine backup. See [recovery](../deploy/backup-restore.md) and [verification records](NOTES.md).
