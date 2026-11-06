# Engineering notes: implementation, corrections and verification

The connector, analytical model, Django backend, browser, tests and documentation used AI assistance, including delegated authorship and review. Verification combined source review, regression tests, independent calculations and scoped integration checks. The corrections and limits below describe what those checks established.

## 1. Connector: validate and publish coherent observations

Explicit source contracts preserve natural keys, text identifiers, units and exact decimal measures. Pagination requests complete-key ordering, advances by rows received and continues to an empty terminal page. Provider totals are diagnostic. Missing advertised boundaries fail validation; interior gaps and representable outliers remain evidence. Recent extraction replaces a 30-day correction interval, including confirmed deletions, but can miss older revisions. It still rewrites the raw candidate and rebuilds the complete model.

**Correction: Windows synchronization and failure reporting.** Real extraction and integration tests exposed `fsync` on a read-only descriptor. Staging files now use `r+b` for synchronization and are closed before publication. Injected diagnostic I/O failures also showed that reporting could hide the original error; status/artifact writes are now best effort and preserve the primary failure.

After validation, one `data/catalog.sqlite3` transaction advances matching raw/model references and completes ingestion/build records. Before commit, failure leaves the prior pair current. Unfinished staging or immutable candidates can remain; there is no automatic cleanup or promotion. Recovery changes abandoned running records without downgrading committed success. A `validated` artifact or newer directory is not publication authority.

Django refresh-job completion is a separate transaction. An interrupted job can already have published data. [The data guide](DATA_PIPELINE_AND_MODEL.md) and [operations](LOCAL_OPERATIONS.md) explain these boundaries and recovery.

## 2. Analytical model: preserve meaning and verify arithmetic

Separate source facts retain missing observations, nulls and zeros. Generator facts reference identity dimensions so a missing facility-day parent does not discard evidence. Child sums check published observations rather than replace the national operands. The canonical fleet metric uses same-day national values with explicit Decimal rounding.

**Correction: percentage metadata.** Review found absolute percentages labeled as percentage points. Model version 2 corrects labels; the frozen version 1 contract and warning preserve existing artifacts. Logical-hash comparison confirmed unchanged numerical data, and the metric remains version 1.

**Correction: derived Decimal limits.** Documentation overstated outlier preservation: valid source operands can overflow a derived type. For example, `10000 / 0.000000001` exceeds the stored share's envelope. Ingestion-pipeline regressions with synthetic inputs cover share and quantization overflow. The complete candidate fails with `invalid_model`, preserving the prior raw/model pair instead of clipping or returning an ordinary null.

Independent acceptance compared raw observations with modeled facts, recalculated the metric at higher precision and reaggregated reconciliation values. Those are retained results, not a new extraction. [Findings](FINDINGS.md) owns concrete observations and cause qualifications; [the model guide](DATA_PIPELINE_AND_MODEL.md) owns schemas, arithmetic and verification limits.

## 3. Backend: separate authorization from execution safety

Django derives the product persona and checks persisted identity. Every named table is authorized before analytical I/O, including references inside unsupported query shapes. Login admission uses persisted hashed account/address buckets with a shared lock and database transaction; password checking remains separate. Identity-bound caches and cursors are reauthorized before delivery.

**Correction: parser validation was mistaken for isolation.** Security review rejected reliance on SQLGlot and engine settings alone. Compilation now runs in a bounded disposable Python child; regenerated SQL executes over authorized checksummed bytes in a separate BUFFER-only DuckDB-WASM/Node process. Direct probes bypass parser rejection to test file/network restrictions. Native DuckDB remains trusted transformation/report code, with no user-query fallback. The Python compiler is resource-bounded, not filesystem-jailed.

**Correction: cached session fields survived logout.** Independent review reproduced result delivery after deletion of the persisted session. Final policy now checks the actual unexpired Session row and user/backend/authentication binding. Delete/expire/corrupt-session and real same-session logout regressions require `401` and no cache entry.

Process admission, memory/CPU/deadline controls and actual-child cleanup tests address different risks. A result `LIMIT` does not bound scan, sort or aggregate cost. [Query security](QUERY_SECURITY.md) and [the API contract](API_CONTRACT.md) define the complete controls and their trust assumptions.

## 4. Browser: preserve model and session coherence

All views share one pinned model; newer publications require explicit adoption. Exact values remain text in tables and exports. Charts present the stored metric, preserving zero and gaps. Client state never grants permissions.

**Correction: stale session and refresh replies changed visible state.** Timing tests exposed a focus return inheriting an unprotected background check, history retained across replacement sessions, and delayed refresh history overwriting a newly accepted job. Protected checks now latch/escalate, obsolete storage scopes retire, and generation/job-ID guards reject stale updates. Unknown network outcomes retain a retry shield.

Historical browser acceptance exercised real backend/worker paths alongside controlled failure scenarios. Keyboard, visual, contrast and narrow-layout checks complemented automated accessibility scans. These results are version-specific; full browser acceptance was not rerun during the source-documentation pass. [Browser workspace](BROWSER_WORKSPACE.md) records the maintained behavior and remaining usability limits.

## 5. Verification status and remaining limits

| Check | Recorded result and scope |
| --- | --- |
| Latest recorded full Python suite (November 2, 2026) | **654 passed, 1 failed, 1 skipped**, Python 3.14.2; zero setup errors. A targeted retry reproduced the failure. |
| Documentation pass at `8d6d02c` | **255 Python tests passed** across domain, EIA client, SQL policy/compiler and reports; **17 JavaScript units passed**. These limited checks do not replace the full suite or full browser acceptance. |
| Documentation validation | Executable Python ASTs, JavaScript ASTs/tokens, SQL names/tokens/parameters and behavior-bearing markup tokens were preserved. Scoped syntax, lint and formatting checks passed. |

The unresolved test requests a **50 ms** total budget and receives the expected timeout, but assumes a child launched. Runtime discovery, file reading and hashing can exhaust that budget before launch. An empty process list does not demonstrate a leaked child; the assertion remains unfixed. Prelaunch expiry and established-child cleanup need separate deterministic tests. The Windows symlink-privilege skip is not a passed boundary test; long staging paths also exposed portability limits.

Synthetic tests, partial recorded first pages, live extraction, independent calculations and browser/package acceptance establish different things. Historical credential audits are scoped checks, and configured CI does not establish a completed remote run. SQL comments affect future full-text report hashes; historical hashes are not rewritten.

File synchronization, SQLite WAL and `synchronous=FULL` strengthen local durability without proving universal power-loss protection. Directory synchronization is platform-dependent, and immutable files are not off-machine backups. Automatic reference-aware retention, tested restore, equivalent POSIX runtime acceptance, hosted operation, larger-volume/load performance and broader assistive-technology testing remain unverified or future work. Complete rebuilds and whole-file query transfer remain scaling limits. Reproduction commands belong in [local operations](LOCAL_OPERATIONS.md).
