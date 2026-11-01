# API contract

The same-origin JSON API is served under `/api/v1` without trailing slashes. The executable schema is [OpenAPI 3.1](../src/outage_explorer/service/openapi.json), also available after login at `GET /api/v1/openapi.json`. [SECURITY.md](SECURITY.md) explains the enforcement boundaries; [DATA_MODEL.md](DATA_MODEL.md) defines the datasets and metric.

Use the [README](../README.md) for installation and the local operator commands. No endpoint accepts an EIA key, filesystem path, caller-selected persona, or arbitrary connection string. Browser authentication uses Django's `sessionid` cookie.

## Capabilities

| Method and path | Access | Successful result |
| --- | --- | --- |
| `GET /auth/csrf` | Public | `200`, CSRF bootstrap token and cookie |
| `POST /auth/login` | Public, valid CSRF | `200`, user/persona and rotated CSRF token |
| `POST /auth/logout` | Signed in, valid CSRF | `200`, `{"signed_out":true}` |
| `GET /auth/session` | Signed in | `200`, current username/persona |
| `GET /catalog` | Signed in | `200`, permitted logical dataset metadata |
| `GET /data/{dataset_id}` | Dataset permission | `200`, filtered, ordered preview page |
| `POST /query` | Dataset permission, valid CSRF | `200`, one page of a bounded SQL result |
| `GET /refresh` | Active Admin | `200`, that Admin's own recent jobs |
| `POST /refresh` | Active Admin, valid CSRF | `202` new job; `200` idempotent replay |
| `GET /refresh/{job_id}` | Active Admin who owns the job | `200`, durable job state |
| `GET /health/live` | Public | `200`, `{"status":"ok"}` |
| `GET /health/ready` | Active Admin | `200`, published model/runtime availability |
| `GET /openapi.json` | Signed in | `200`, static API schema |

Paths in the table are relative to `/api/v1`. Viewer may access **`national_daily` and `fleet_daily` only**. Analyst and Admin may access all ten registered analytical datasets. Admin additionally owns refresh and readiness capabilities; it does not receive another Admin's jobs. Unknown and forbidden dataset IDs receive the same `403 dataset_forbidden` response before analytical file access. The catalog omits unauthorized datasets and foreign-key references to hidden datasets. The static OpenAPI document describes the whole API; it does not grant permission or reveal another persona's runtime rows/catalog metadata.

## Browser session and CSRF

1. `GET /api/v1/auth/csrf`; retain browser cookies and the JSON `csrf_token`.
2. `POST /api/v1/auth/login` with `Content-Type: application/json`, `X-CSRFToken: <bootstrap token>`, and `{"username":"analyst","password":"<operator-generated password>"}`.
3. Keep the returned session cookie and replace the header token with the **new** `csrf_token` returned after login.
4. Send that header and the browser cookies on **every POST**, including `/query`, `/refresh`, `/auth/logout`, and subsequent login attempts. Logout's JSON body is `{}`.

For `fetch`, use same-origin credentials. The CSRF response token is a Django token; do not assume it is textually identical to the cookie. Session cookies are HttpOnly and SameSite=Lax. Sessions expire after 1,800 seconds and at browser close. Changing a password, deleting the persisted session, disabling the user or changing the persona is checked against current server state. Refresh or query cursors are not authentication credentials.

An incorrect username/password returns the same `401 invalid_credentials`. Shared login throttles allow 10 attempts per username and 30 per client address in a five-minute window; successful attempts count too. A limit returns `429` with `Retry-After`. Raw usernames and addresses are not stored in rate-limit bucket keys.

## Request and scalar encoding

JSON bodies must be one UTF-8 object with the endpoint's supported fields. Duplicate keys, unknown fields and non-finite JSON constants are rejected. JSON bodies are limited to **32 KiB**; SQL input and regenerated SQL are each limited to **16 KiB UTF-8**. Filtered endpoints reject duplicate/unknown query parameters. Identifiers and model IDs must come from the catalog; a model ID is 32 lowercase hexadecimal characters. A missing/unavailable model returns `503 data_unavailable`, while a malformed ID returns `400 invalid_model_id`.

Results use a positional matrix: `columns` supplies each column's `name` and SQL `type`; `rows` contains arrays in the same order. Exact decimals are **fixed-point strings**, and 64-bit integer values are strings so JavaScript does not silently round them. Smaller integers, finite floating-point values and booleans use JSON scalars. Dates use `YYYY-MM-DD`, timestamps use ISO 8601, and absent scalar values use JSON `null`. Nested/list/binary results are outside the result contract. An empty result is `rows: []`, not an error.

Every response has `Cache-Control: no-store` and `X-Request-ID`. The former controls browser/shared HTTP caches; the separate authorized server query cache is described below. The maximum JSON response is **2 MiB**.

## Catalog and previews

`GET /api/v1/catalog?model_id=<optional model>` returns `model_id`, `source_snapshot_id`, `model_version`, `datasets`, `metrics`, and `warnings`. Dataset descriptors include logical columns/types, PK/FK metadata, units, classification, row/date bounds and allowed personas. Physical paths and Parquet filenames are not exposed. Omitting `model_id` chooses the current published model. Historical contracts retain their own metadata and any version warning.

`GET /api/v1/data/{dataset_id}` accepts:

| Parameter | Contract |
| --- | --- |
| `start`, `end` | Optional inclusive ISO dates; `start <= end`; only datasets with `period` |
| `facility_id` | Optional exact string match, maximum 64 characters; only datasets with that column |
| `limit` | Page size, integer 1–1,000; default 100 |
| `model_id` | Optional immutable model pin |
| `cursor` | Signed continuation token from the preceding page |

For example, an Analyst can request `/api/v1/data/generator_daily?start=2026-09-01&end=2026-09-30&facility_id=3166&limit=100`. Filtering a dataset that lacks the requested date/facility column returns `400 unsupported_filter`.

A preview returns `dataset_id`, both publication IDs, `columns`, `rows`, `returned_rows`, `next_cursor`, and the effective `filters`. Rows are ordered by the dataset's full primary key. Pagination uses **keyset continuation**, not a caller-controlled SQL offset. Repeat the original filters and limit and add the returned cursor; stop when `next_cursor` is null. Preview pages can traverse the permitted dataset beyond 1,000 total rows; the limit applies to each page.

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

This example uses the pinned model analyzed in [FINDINGS.md](../FINDINGS.md):

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

The selected source is authorized first. A separate, bounded Python compiler process discovers all named references, validates the SQLGlot AST allowlist and regenerates SQL; the web process authorizes every returned reference before opening analytical data. References inside unsupported nested constructs are included, so an unauthorized reference can return `403` before the shape is rejected as unsupported SQL. Original submitted SQL is not executed directly. The compiler has a two-second subdeadline and no in-process fallback. The grammar is explicit in [sql_policy.py](../src/outage_explorer/sql_policy.py); its process boundary is [sql_compiler.py](../src/outage_explorer/sql_compiler.py).

The compiler appends primary-key or group-key tie breakers to support reproducible paging over immutable data. An explicit SQL `LIMIT` keeps its ordinary meaning. The server retains at most `max_rows` plus one internal sentinel; `truncated: true` means that sentinel proved more rows existed beyond the total result cap. A result cap limits returned rows, not all work needed for an aggregate or sort.

To continue, repeat the same effective SQL, dataset, cap, page size and optional model pin, adding `next_cursor` to the JSON body. SQL pagination slices the **same capped ordered result**; it does not return rows beyond the cap. The next page normally uses the authorized result cache. If that cache expires or the publication changes, the server may recompute the same bounded query against the cursor's pinned model.

Both preview/query cursors expire after **15 minutes** and bind the session/authorization context, dataset, effective request and model. Changing filters, page size, query or an explicitly supplied model invalidates the cursor. They are signed, not encrypted; treat them as opaque protocol state. A new publication does not silently redirect an existing cursor to different data.

The server query cache lasts **60 seconds**, retains up to **eight entries per user**, and is scoped to user, persona, session/authentication state, canonical SQL, selected model and current publication generation. Authorization is checked before cache use and before delivery. Another user's/session's cache is not shared, and changing permissions invalidates the authorization binding. A cache hit does not extend its original expiry.

Queries and previews, **including cache hits**, use process-shared admission: **one pending/executing request per account, two execution slots, eight waiting slots, and a five-second maximum queue wait**. Saturation returns `429` and `Retry-After: 1`. Every request recompiles and reauthorizes before using its cache. Compilation and engine execution share an independent **eight-second execution budget**, including their startup/input preparation; the compiler receives at most two seconds of that budget. Queue waiting is excluded. Results have a 1,000-row client cap and 2 MiB output cap. See [SECURITY.md](SECURITY.md) for memory/CPU boundaries and platform qualifications.

## Asynchronous refresh

An active Admin submits `POST /api/v1/refresh` with `{"mode":"recent"}` or `{"mode":"full"}`. Omitted mode defaults to `recent`; no date, key or path override is accepted. The optional `Idempotency-Key` header is 1–128 case-sensitive ASCII characters from letters, digits, `.`, `_`, `:`, `-`.

`202` means the durable job was **queued**, not that new data is available. The body includes `id`, `mode`, `status`, creation/start/finish times, `error_code`, safe `message`, `result`, and `status_url`; `Location` points to that status endpoint. Poll it as the same Admin. `GET /api/v1/refresh?limit=10` lists only that Admin's jobs; `limit` is 1–50. A missing or differently owned job returns `404 job_not_found`.

The same owner/key/mode returns the original job with `200`, including after completion/failure. Reusing the key with a different mode returns `409 idempotency_conflict`. Use a new key for an intentional new operation. Only one job may be queued/running globally; competing new requests return `409 refresh_busy` and `Retry-After: 5`.

The operator must run the separate worker (`python -m outage_explorer.web.local worker`, optionally `--once`). There are no background refresh threads in HTTP processes. The worker rechecks the owner's active Admin persona before ingestion. States are `queued → running → succeeded|failed|interrupted`. Success records the matching `snapshot_id`, `model_id`, and mode, then attempts cache cleanup; generation keys also prevent stale cache reuse.

Extraction/model validation failures preserve the previously published raw/model pair. A worker that dies after publication but before recording job completion can leave an `interrupted` job **even though publication occurred**. Review current/model history before retrying; job interruption is not proof of rollback. Restart recovery requires the independent worker OS lock, so a second live worker cannot mark an active job interrupted.

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
