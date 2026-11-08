# Use the JSON API: requests, results and errors

The API is the contract between the browser workspace and Django: a client asks for a permitted dataset or a bounded SQL result, and the server returns JSON. JSON is structured text; a session cookie identifies the signed-in user, while a CSRF token protects POST requests made with that cookie. Start with [local setup](LOCAL_OPERATIONS.md) for accounts and a running service, then follow the authentication sequence below.

Every path below is under `/api/v1` without a trailing slash. A same-origin client uses the same scheme, host and port as the application. The executable schema is [OpenAPI 3.1](../src/outage_explorer/service/openapi.json), also available after login at `GET /api/v1/openapi.json`. The [query security guide](QUERY_SECURITY.md) explains enforcement boundaries; the [data pipeline and model guide](DATA_PIPELINE_AND_MODEL.md) defines the datasets and metric.

Use the [local operations guide](LOCAL_OPERATIONS.md) for installation and the local operator commands. No endpoint accepts an EIA key, filesystem path, caller identity override, or arbitrary connection string. Dedicated Admin account-management endpoints may assign another account a product persona; analytical requests cannot choose their own permissions. Browser authentication uses Django's `sessionid` cookie.

## Capabilities

| Method and path | Access | Successful result |
| --- | --- | --- |
| `GET /auth/csrf` | Public | `200`, CSRF bootstrap token and cookie |
| `POST /auth/login` | Public, valid CSRF | `200`, user/persona and rotated CSRF token |
| `POST /auth/logout` | Signed in, valid CSRF | `200`, `{"signed_out":true}` |
| `GET /auth/session` | Signed in | `200`, current username/persona and opaque session scope |
| `GET /catalog` | Signed in | `200`, permitted logical dataset metadata |
| `GET /data/{dataset_id}` | Dataset permission | `200`, filtered, ordered preview page |
| `POST /data/{dataset_id}` | Dataset permission, valid CSRF | `200`, structured full-dataset search/filter/sort seek page |
| `POST /query` | Dataset permission, valid CSRF | `200`, one page of a bounded SQL result |
| `GET /users` | Active Admin | `200`, searchable account page without credentials |
| `POST /users` | Active Admin, valid CSRF | `201`, new account and one-time generated password |
| `PUT/PATCH /users/{user_id}` | Active Admin, valid CSRF | `200`, updated profile/access fields |
| `POST /users/{user_id}/password` | Active Admin, valid CSRF | `200`, one-time generated replacement password |
| `DELETE /users/{user_id}` | Active Admin, valid CSRF | `200`, confirmed deletion |
| `GET /refresh` | Active Admin | `200`, that Admin's own recent jobs |
| `POST /refresh` | Active Admin, valid CSRF | `202` new job; `200` idempotent replay |
| `GET /refresh/{job_id}` | Active Admin who owns the job | `200`, durable job state |
| `GET /health/live` | Public | `200`, `{"status":"ok"}` |
| `GET /health/ready` | Active Admin | `200`, published model/runtime availability |
| `GET /openapi.json` | Signed in | `200`, static API schema |

Paths in the table are relative to `/api/v1`. Viewer may access **`national_daily` and `fleet_daily` only**. Analyst and Admin may access all ten registered analytical datasets. Admin additionally manages product accounts and owns refresh and readiness capabilities; it does not receive another Admin's jobs. Unknown and forbidden dataset IDs receive the same `403 dataset_forbidden` response before analytical file access. The catalog omits unauthorized datasets and foreign-key references to hidden datasets. The static OpenAPI document describes the whole API; it does not grant permission or reveal another persona's runtime rows/catalog metadata.

## Browser session and CSRF

1. `GET /api/v1/auth/csrf`; retain browser cookies and the JSON `csrf_token`.
2. `POST /api/v1/auth/login` with `Content-Type: application/json`, `X-CSRFToken: <bootstrap token>`, and `{"username":"analyst","password":"<operator-generated password>"}`.
3. Keep the returned session cookie and replace the header token with the **new** `csrf_token` returned after login.
4. Send that header and the browser cookies on **every POST, PUT, PATCH and DELETE**, including `/query`, `/refresh`, `/auth/logout`, and subsequent login attempts. Logout's JSON body is `{}`.

For `fetch`, use same-origin credentials. The CSRF response token is a Django token; do not assume it is textually identical to the cookie. Session cookies are HttpOnly and SameSite=Lax. Sessions expire after 1,800 seconds and at browser close. Changing a password, deleting the persisted session, disabling the user or changing the persona is checked against current server state. Account-management access changes increment a persisted session version, so reversing a role/active-state change does not restore a previously revoked session. Refresh or query cursors are not authentication credentials.

`GET /api/v1/auth/session` also returns top-level `session_scope`: an opaque 64-character lowercase hexadecimal HMAC shared with the protected HTML bootstrap. It binds browser state to the current persisted session/authentication/persona, so a new session for the same username and role is distinguishable. It stays stable when analytical data is refreshed. The browser uses it to retire old query-history scopes and revalidate restored pages. It is neither a credential nor a cursor: authentication still requires the valid session cookie, and sending this value does not grant access. The exact response is defined by OpenAPI's `SessionResponse`; see [browser workspace](BROWSER_WORKSPACE.md) for its client use.

An incorrect username/password returns the same `401 invalid_credentials`. Shared login throttles allow 10 attempts per username and 30 per client address in a five-minute window; successful attempts count too. A limit returns `429` with `Retry-After`. Raw usernames and addresses are not stored in rate-limit bucket keys.

## Admin account management

User management is a product Admin capability, separate from Django's staff/superuser administration. It is available even before an analytical model has been published. `GET /api/v1/users` searches manageable accounts by username and names, returning ID-ordered pages with default `page_size=25` and maximum 100. `next_cursor` is null at the end; otherwise repeat the same search and page size with that token. Tokens expire after 15 minutes and are limited to 4,096 characters. Infrastructure staff/superuser accounts are excluded. Public account records contain ID, username, first/last names, product role, active state, timestamps, `is_self`, and an opaque `version`; they contain no password hash or session credential.

`POST /api/v1/users` requires `username` and `role`; `first_name`, `last_name` and `is_active` are optional. Creation generates a 32-character URL-safe random password with 192 bits of entropy and returns `{user, generated_password, reauthenticate:false}` with status 201. There is no caller-supplied password field. Only the password hash is persisted. The plaintext is returned once in this no-store response and must be shared with the recipient privately; a lost password can be replaced, not retrieved.

`PUT` and `PATCH /api/v1/users/{user_id}` update only supplied username, name, role and active-state fields. Every edit, reset and delete requires the current `version`. A stale version returns 409 so a second administrator cannot silently overwrite newer account changes. Usernames are NFKC-normalized using Django's Unicode validator and reserved case-insensitively across all accounts. Duplicate usernames also conflict. Editing names or username preserves sessions; changing role or active state permanently revokes the affected account's existing sessions. Account permissions still derive from the authenticated caller, not the target ID or supplied assignment.

`POST /api/v1/users/{user_id}/password` accepts `{version}` and returns a new one-time `generated_password`, replacing the old hash and revoking existing sessions. A self-reset returns `reauthenticate:true`: retain the new password before acknowledging the credentials dialog and signing in again. Generated credentials are never written into the local seed-password configuration, browser history or saved views.

`DELETE /api/v1/users/{user_id}` accepts `{version}` and returns `{deleted:true}`. Deletion removes the account's saved views and cached query results. Historical refresh records remain with no account owner; an account with queued/running refresh work cannot be deleted until that work finishes. Self-deletion, self-deactivation and self-demotion are rejected, and the last active Admin is protected. These rules are checked inside the serialized database mutation boundary, independently of disabled browser buttons. Contention returns `503 users_busy` with `Retry-After: 1`.

## Request and scalar encoding

JSON bodies must be one UTF-8 object with the endpoint's supported fields. Duplicate keys, unknown fields and non-finite JSON constants are rejected. JSON bodies are limited to **32 KiB**; SQL input and regenerated SQL are each limited to **16 KiB UTF-8**. Filtered endpoints reject duplicate/unknown query parameters. Identifiers and model IDs must come from the catalog; a model ID is 32 lowercase hexadecimal characters. A missing/unavailable model returns `503 data_unavailable`, while a malformed ID returns `400 invalid_model_id`.

Results use a positional matrix: `columns` supplies each column's `name` and SQL `type`; `rows` contains arrays in the same order. Exact decimals are **fixed-point strings**, and 64-bit integer values are strings so JavaScript does not silently round them. Smaller integers, finite floating-point values and booleans use JSON scalars. Dates use `YYYY-MM-DD`, timestamps use ISO 8601, and absent scalar values use JSON `null`. Nested/list/binary results are outside the result contract. An empty result is `rows: []`, not an error.

Every response has `Cache-Control: no-store` and `X-Request-ID`. The former controls browser/shared HTTP caches; the separate authorized server query cache is described below. The maximum JSON response is **2 MiB**.

## Catalog and previews

`GET /api/v1/catalog?model_id=<optional model>` returns `model_id`, `source_snapshot_id`, `model_version`, `datasets`, `metrics`, and `warnings`. Dataset descriptors include logical columns/types, PK/FK metadata, units, classification, row/date bounds and allowed personas. Physical paths and Parquet filenames are not exposed. Omitting `model_id` chooses the current published model. Historical contracts retain their own metadata and any version warning.

`POST /api/v1/data/{dataset_id}` accepts a structured JSON specification (32 KiB maximum) with session and CSRF protection. `GET` remains available for simple query parameters within an 8,192-character encoded query budget. Neither method accepts SQL.

| Parameter | Contract |
| --- | --- |
| `start`, `end` | Optional inclusive ISO dates; `start <= end`; only datasets with `period` |
| `facility_id`, `generator_id` | Optional exact string matches, maximum 64 characters; only datasets with those columns; leading zeros are preserved |
| `search` | Case-insensitive literal substring across every exposed column value, maximum 256 characters; `%` and `_` are ordinary characters |
| `sort`, `direction` | Declared column and `asc`/`desc`; default first PK ascending; remaining PK columns break ties ascending; nulls always last |
| `filters` | Up to eight typed AND conditions: `{column, op, value?, upper?}`; array in POST, JSON-encoded text in GET |
| `limit` | Page size, integer 1–1,000; default 100 |
| `model_id` | Optional immutable model pin |
| `cursor` | Signed continuation token from the preceding page |

For example, an Analyst can request `/api/v1/data/generator_daily?start=2026-09-01&end=2026-09-30&facility_id=3166&limit=100`. Filtering a dataset that lacks the requested date/facility column returns `400 unsupported_filter`.

A browse response returns `dataset_id`, both publication IDs, `columns`, `rows`, `returned_rows`, `next_cursor`, legacy quick `filters`, and canonical applied `query` (`search`, sorted/deduplicated `filters`, complete `order`). Search, filters and ordering apply to the **whole selected dataset before paging**. `returned_rows` counts this page, not all matches; no matching `COUNT(*)` runs on every request.

Conditions support `eq`/`ne`; numeric/date columns also support `lt`, `lte`, `gt`, `gte`, and inclusive `between`. Literal text conditions support `contains`, `not_contains`, and `starts_with` and match case; global search ignores case using DuckDB's Unicode `lower`. Every type supports `is_null`/`not_null` without values. `between` requires `value` and `upper`; other value predicates require only `value`. Decimal/date operands use exact strings without rounding or exponent syntax; booleans accept `true`/`false`. All conditions AND together, including quick dates/IDs. Dates and numbers participate in global search through their canonical database string representation. No accent folding, Unicode normalization, regular-expression or wildcard interpretation occurs.

```json
{"search":"station","sort":"outage_mw","direction":"desc","filters":[{"column":"capacity_mw","op":"gte","value":"100.25"},{"column":"outage_mw","op":"not_null"}],"limit":100}
```

Signed seek cursors preserve exact sort values and unique primary-key ties. Repeat the effective specification and limit, adding the cursor; stop when it is null. Equivalent predicate order and numeric spellings normalize together. Tokens bind actor/session/persona, query, page size and immutable source/model publication for 15 minutes. A new current publication does not redirect a pinned investigation. Pages can traverse beyond 1,000 total matches without OFFSET or browser result accumulation. POST cursors are bounded to 16,384 characters; GET retains its 4,096-character bound. Oversized sort values return a safe `413` instead of a truncated continuation; use POST or another sort column.

Structured browsing uses an isolated native DuckDB reader with Parquet projection/predicate pushdown and bounded top-N ordering. Search and arbitrary sort/seek combinations may still scan qualifying input; there is no text or seek index. It verifies SHA-256 on a bounded stat-identity cache miss and checks identity on every request. Admission, deadlines, response caps and final authorization checks remain shared. See the [structured browse boundary](QUERY_SECURITY.md#structured-browsing-has-a-separate-trusted-reader). User-written SQL keeps the restricted compiler/WASM path below.

### Explore interaction

Explore keeps three separate states: the last applied database specification, its bounded loaded page, and current form/text edits. **Filter loaded rows** immediately narrows only that page in memory, including input, paste and cut. Typing and plain Enter send no analytical request. Matching is case-insensitive and literal over individual canonical cell values: ISO dates, exact unformatted numbers, textual IDs and `true`/`false`. Nulls do not supply searchable text. Outer whitespace remains literal, matching the database API; display-only date/number formatting is not an additional search representation.

**Apply query** explicitly submits the current text and typed conditions to the full dataset, resetting continuation history. Conditions, date scope and entity IDs share one collapsible field/operator/value editor. Next/Previous and header sorting always retain the applied database specification, even when edits are pending. Local text remains in place across new pages and sorting; a completed request is filtered using the latest text without overwriting it or promoting it into that request.

The grid reports **shown rows out of loaded rows**, independently of the dataset's metadata total. Zero local matches do not imply zero database matches and do not discard a raw page's continuation. **Clear filter** restores the loaded page; **Reset query** clears the draft clauses/text and restores primary-key ordering. Both require Apply before changing an applied database search. **Export visible rows** exports the locally visible subset, in the displayed server order, preserving exact values and CSV protections. A new dataset resets this local state; explicit model adoption resets cursors while preserving compatible draft intent separately from the applied query.

## SQL queries

`POST /api/v1/query` accepts exactly these fields:

| Field | Contract |
| --- | --- |
| `dataset_id` | Required permitted logical source |
| `sql` | Required supported SELECT against that source |
| `max_rows` | Total retained result cap, 1–1,000; default 1,000 |
| `page_size` | Rows in this response, 1–`max_rows`; default `min(100, max_rows)` |
| `model_id` | Optional immutable model pin |
| `cursor` | Optional signed continuation for this effective query |

The default page size automatically decreases when the total result cap is below 100.

This example uses the pinned model analyzed in [data findings](FINDINGS.md):

```json
{
  "dataset_id": "fleet_daily",
  "sql": "SELECT period, offline_pct FROM fleet_daily WHERE period BETWEEN DATE '2026-09-01' AND DATE '2026-09-30' ORDER BY period",
  "max_rows": 30,
  "page_size": 1,
  "model_id": "e5721cad365d47beb0a8a322199a3138"
}
```

The first page has this shape; the cursor is shown as a placeholder:

```json
{
  "dataset_id": "fleet_daily",
  "model_id": "e5721cad365d47beb0a8a322199a3138",
  "source_snapshot_id": "981a8f4ed5214ef6be4ad3cc5b4eee01",
  "columns": [{"name":"period","type":"DATE"},{"name":"offline_pct","type":"DECIMAL(24,9)"}],
  "rows": [["2026-09-01","2.780404731"]],
  "returned_rows": 1,
  "result_rows": 30,
  "truncated": false,
  "next_cursor": "<opaque signed token>",
  "cache_hit": false,
  "cache_ttl_seconds": 60,
  "max_rows": 30
}
```

The supported language is an explicit **single-table SELECT subset**, not unrestricted DuckDB SQL:

- Projection, aliases, `WHERE`, comparisons/boolean expressions, arithmetic, `BETWEEN`, literal `IN`, `LIKE`/`ILIKE`, `IS NULL`, `GROUP BY`, `HAVING`, `ORDER BY`, and bounded `LIMIT`.
- `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, aggregate `DISTINCT`, `ABS`, `ROUND`, `COALESCE`, and supported scalar casts. For example, `SELECT COUNT(*) AS days FROM fleet_daily` is valid.
- No joins, CTEs, subqueries, windows, set operations/`UNION`, `SELECT DISTINCT`, SQL `OFFSET`, DDL/DML, `COPY`, `ATTACH`, `PRAGMA`, `SET`, extensions, table/file/network functions, or arbitrary user-defined functions. Constructs such as `CASE` and `NULLIF` are also outside this implemented subset.

The selected source is authorized first. A separate, bounded Python compiler process discovers all named references, validates the SQLGlot AST allowlist and regenerates SQL; the web process authorizes every returned reference before opening analytical data. References inside unsupported nested constructs are included, so an unauthorized reference can return `403` before the shape is rejected as unsupported SQL. Original submitted SQL is not executed directly. The compiler has a two-second subdeadline and no in-process fallback. The grammar is explicit in [sql_policy.py](../src/outage_explorer/query/sql_policy.py); its process boundary is [sql_compiler.py](../src/outage_explorer/query/sql_compiler.py).

The compiler appends primary-key or group-key tie breakers to support reproducible paging over immutable data. An explicit SQL `LIMIT` keeps its ordinary meaning. The server retains at most `max_rows` plus one internal sentinel; `truncated: true` means that sentinel proved more rows existed beyond the total result cap. A result cap limits returned rows, not all work needed for an aggregate or sort.

To continue, repeat the same effective SQL, dataset, cap, page size and optional model pin, adding `next_cursor` to the JSON body. SQL pagination slices the **same capped ordered result**; it does not return rows beyond the cap. The next page normally uses the authorized result cache. If that cache expires or the publication changes, the server may recompute the same bounded query against the cursor's pinned model.

Both preview/query cursors expire after **15 minutes** and bind the session/authorization context, dataset, effective request and model. Changing filters, page size, query or an explicitly supplied model invalidates the cursor. They are signed, not encrypted; treat them as opaque protocol state. A new publication does not silently redirect an existing cursor to different data.

The server query cache lasts **60 seconds**, retains up to **eight entries per user**, and is scoped to user, persona, session/authentication state, canonical SQL, selected model and current publication generation. Authorization is checked before cache use and before delivery. Another user's/session's cache is not shared, and changing permissions invalidates the authorization binding. A cache hit does not extend its original expiry.

Queries and previews, **including cache hits**, use process-shared admission: **one pending/executing request per account, two execution slots, eight waiting slots, and a five-second maximum queue wait**. Saturation returns `429` and `Retry-After: 1`. Every request recompiles and reauthorizes before using its cache. Compilation and engine execution share an independent **eight-second execution budget**, including their startup/input preparation; the compiler receives at most two seconds of that budget. Queue waiting is excluded. Results have a 1,000-row client cap and 2 MiB output cap. See [query security and runtime boundaries](QUERY_SECURITY.md) for memory/CPU boundaries and platform qualifications.

## Asynchronous refresh

An active Admin submits `POST /api/v1/refresh` with `{"mode":"recent"}` or `{"mode":"full"}`. Omitted mode defaults to `recent`; no date, key or path override is accepted. The optional `Idempotency-Key` header is 1–128 case-sensitive ASCII characters from letters, digits, `.`, `_`, `:`, `-`.

`202` means the durable job was **queued**, not that new data is available. The body includes `id`, `mode`, `status`, creation/start/finish times, `error_code`, safe `message`, `result`, `progress`, and `status_url`; `Location` points to that status endpoint. Poll it as the same Admin. `GET /api/v1/refresh?limit=10` lists only that Admin's jobs; `limit` is 1–50. A missing or differently owned job returns `404 job_not_found`.

`progress` is `{}` for queued or older jobs without measurements. Otherwise it contains an allowlisted `stage`, `updated_at` (the last actual milestone), `rows_received` and `pages_fetched`, with optional logical `route` and per-grain `routes` counts. Received counts include deletion-confirmation rereads; they are not unique published row counts. Extraction totals are unknown, so provider-advertised totals never drive a percentage. Only `exporting_model` includes `completed` and `total` for the fixed set of model outputs; this percentage describes export alone. Source checks, individual model-building phases, model verification and publication each report their current stage. Count-only writes are throttled to one attempt per second, with immediate stage transitions and a final flush. Snapshots survive reloads and remain on failed/interrupted jobs. `updated_at` is not a heartbeat or proof a worker is live; unchanged progress may mean slow work or an unavailable worker. No provider URLs, diagnostics, credentials, SQL or paths are included.

The same owner/key/mode returns the original job with `200`, including after completion/failure. Reusing the key with a different mode returns `409 idempotency_conflict`. Use a new key for an intentional new operation. Only one job may be queued/running globally; competing new requests return `409 refresh_busy` and `Retry-After: 5`.

Direct host operation requires the separate worker (`python -m outage_explorer.web.local worker`, optionally `--once`); the [Docker workflow](DOCKER.md#configure-the-refresh-worker) starts a continuous worker as an app dependency. There are no background refresh threads in HTTP processes. The worker rechecks the owner's active Admin persona before ingestion. States are `queued → running → succeeded|failed|interrupted`. Success records the matching `snapshot_id`, `model_id`, and mode, then attempts cache cleanup; generation keys also prevent stale cache reuse.

A missing or blank worker-only `EIA_API_KEY` ends the claimed job as `failed` with `error_code: "refresh_not_configured"` and a safe configuration message, before extraction or publication. The HTTP enqueue request can still return `202`; the job records the execution outcome. Configure and recreate the worker, then submit a new refresh. Existing failed jobs and their idempotency replay results are preserved. No key value is accepted or returned by this API.

Extraction or candidate-model validation failures before catalog publication preserve the previously published raw/model pair. Unfinished candidate files may remain; they are not automatically promoted or cleaned up. A worker that dies after publication but before recording job completion can leave an `interrupted` job **even though publication occurred**. Review current/model history before retrying; job interruption is not proof of rollback. Restart recovery requires the independent worker OS lock, so a second live worker cannot mark an active job interrupted.

## Errors and health

Errors use `application/problem+json`:

```json
{
  "type": "urn:outage-explorer:error:dataset_forbidden",
  "title": "dataset forbidden",
  "status": 403,
  "detail": "Dataset access is not permitted.",
  "code": "dataset_forbidden",
  "request_id": "0123456789abcdef0123456789abcdef"
}
```

| Status | Typical meaning |
| --- | --- |
| `400` | Invalid JSON/parameters, dates, cursor or model ID |
| `401` | Invalid login, missing/expired/revoked session |
| `403` | CSRF, missing persona, forbidden dataset/Admin capability, changed permission |
| `404` / `405` | Unknown route/owned job; unsupported method (`Allow` supplied) |
| `409` | Refresh already active or idempotency conflict |
| `413` / `415` | Request/result too large; JSON content type required |
| `422` | Unsupported/invalid SQL, execution/type/resource contract failure |
| `429` | Login/query admission capacity exhausted (`Retry-After` supplied) |
| `503` | Published data/integrity/runtime or bounded compiler unavailable |
| `504` | Query execution deadline exceeded |
| `500` | Safe unexpected-error response |

Engine diagnostics, SQL text, credentials and filesystem paths are not returned in error details. Use `request_id` to correlate safe operational logs. Liveness only confirms the HTTP process responds. Admin readiness checks the selected current model reference and pinned runtime availability; it is not a live EIA call, a full workload benchmark, or a guarantee that every future query will fit its resource budget.

Historical implementation reference: [Django CSRF protection](https://docs.djangoproject.com/en/5.2/howto/csrf/). This records the original guidance; no new external-source verification is claimed.

## Named saved views

`GET/POST /api/v1/saved-views`, `GET/PUT/DELETE /api/v1/saved-views/{view_id}` and `POST /api/v1/saved-views/{view_id}/run` manage named SQL/table/chart definitions. Analyst/Admin may create and share; mutations require ownership, and deletion remains available to an owner after a role downgrade when the dataset is still permitted. All reads require owner/audience visibility plus current dataset access. Unauthorized and unknown definitions use the same 404.

Definitions contain a name, dataset, SQL, retained-row cap, optional typed time-chart fields, explicit shared roles and a revision. Private is the default. Result rows are never persisted in a definition. Create/update validates the supported SQL and, for graphs, actual output types. `model_id` pins validation/execution to the reader's adopted publication; `created_model_id` records provenance. PUT, DELETE and run require the current revision. Execution rechecks definition visibility/revision after the existing SQL sandbox returns, using the reader's session and permissions throughout.

The list uses signed actor/limit-bound creation-order cursors (15-minute expiry), default20/max50 items; each owner may retain50 definitions. Request/response, query admission, SQL grammar, 1,000-retained-row and sandbox limits remain in force. See the packaged OpenAPI document for exact strict body and response schemas.
