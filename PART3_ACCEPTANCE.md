# Part 3 acceptance — authenticated analytical backend

Part 3 is implemented as a locally runnable Django API over the existing immutable model. The core requirements are complete. The delivery also adds a private expiring cache, aggregate queries and capped pagination, resource isolation, asynchronous refresh, OpenAPI, consistent errors and operational health/logging. Broader SQL and identity federation are not claimed.

## Requirement evidence

| PDF requirement / story | Implemented behavior | Authoritative evidence |
| --- | --- | --- |
| Authentication; one seeded user per persona | Django sessions/password hashing; seeded `viewer`, `analyst`, `admin`; no hardcoded password or implicit staff privileges | `web.local init`, `seed_users`, setup/operator tests; real HTTP login with all three generated accounts |
| Catalog / US-02 | Authenticated logical datasets, columns/types/keys and metric definitions; Viewer receives only national/fleet metadata | `service.policy`, `api_services.catalog`; actual HTTP returned 2 Viewer datasets and 10 Analyst/Admin datasets |
| Filtered data / US-03 | Typed Parquet preview with ISO date/facility filters, stable keyset pagination and immutable model pin | Real September preview; synthetic date/facility/decimal, empty-result and refresh-between-pages tests |
| Read-only query / US-04 | Defined single-table SELECT subset, safe aggregates, all references checked, regenerated SQL only, 1,000 retained rows and 2 MiB response ceiling | SQL policy tests, isolated compiler tests, real installed-wheel aggregate/count queries, cap/pagination tests |
| Authorization before every data/query path / US-05 | Server-derived active persona; selected dataset and every discovered reference authorized before analytical access; persisted session/role rechecked before delivery/cache | Adversarial API tests spy on model/execution boundaries; nested/CTE/JOIN references cannot conceal forbidden detail; in-flight revocation regressions |
| Admin-only refresh / US-06 | Durable asynchronous job, separate credential-owning worker, owner-only status/history, idempotency and one active refresh | Refresh tests and genuine queued HTTP→worker→EIA→publication→HTTP acceptance |
| Secrets outside code | Environment-owned EIA key; explicit local setup creates private ignored random Django/demo credentials; API launcher strips upstream/seed variables | `.env.example`, private-file ACL check, operator/environment tests and actual-secret persistence audit |
| Automated unauthorized/write rejection | Authentication/persona/CSRF, write/config/function attacks, cache/cursor isolation and revocation exercised independently | `test_api_security.py`, `test_session_revocation.py`, `test_api_validation.py`, `test_sql_policy.py`, `test_query_sandbox.py` |

The API contract is [docs/API.md](docs/API.md), machine-readable [OpenAPI 3.1](src/outage_explorer/service/openapi.json), and [docs/SECURITY.md](docs/SECURITY.md). `DECISIONS.md` explains synchronous/asynchronous behavior, accepted/rejected SQL, table discovery and the execution boundary. `NOTES.md` records actual generated-code failures and how review/tests caught them.

## Architecture and security invariants

The public service uses Django sessions plus one product persona per user. Product Admin is not Django staff/superuser. No request supplies a role, physical path, EIA key or arbitrary connection. CSRF applies to login, logout, query and refresh; successful login rotates its token. Password changes, account deactivation, role changes, server-side session deletion and session expiry are checked from persisted state, including while work is in flight.

SQL compilation occurs in a disposable, time/memory-limited Python process. It discovers references before validating supported shape; the web service authorizes every returned name before opening analytical data. Only regenerated SQL reaches a separate DuckDB-WASM process. That process receives one checksummed Parquet buffer rather than the model directory or host path. Its custom BUFFER-only imports deny host files/network; bootstrap supplies one pinned local extension before configuration lockdown. Node's permission mode, absence of inherited secrets and engine settings add separate protections. There is no native-engine or in-process parser fallback.

Windows Job Objects enforce 512 MiB, six seconds user CPU, one process and kill-on-close for each child. WASM memory is independently limited to 256 MiB and V8 old heap to 96 MiB. Two admitted operations, one per user and eight waiting positions are shared across HTTP processes through OS locks. Queue wait is at most five seconds. Compilation gets at most two seconds within the shared eight-second admitted execution budget. Output/row limits are independent of SQL LIMIT. Children are killed and reaped on deadline/overflow/failure.

The cache expires after 60 seconds, retains eight entries per user and binds user/session/persona, canonical query, selected model and current publication generation. Permissions precede lookup and repeat before delivery. Refresh changes the generation even if physical cache cleanup fails. Signed cursors expire after 15 minutes and preserve the selected immutable model. Preview uses full-PK keysets; SQL pagination pages within its total retained-row cap.

## Observed HTTP and live refresh

A real Waitress server was exercised at `http://127.0.0.1:8000` using the generated private accounts and actual published data. This was a socket-level HTTP check, separate from Django test-client tests.

- Anonymous catalog, preview, session and readiness returned 401; login without CSRF returned 403. Errors carried matching body/header request IDs.
- Viewer catalog contained exactly `national_daily` and `fleet_daily`. Detail preview, a query referencing generator detail and refresh all returned 403.
- Analyst and Admin catalogs each contained ten datasets. An Analyst facility-filtered generator aggregate returned an exact decimal string; DELETE returned 422.
- National count returned exact BIGINT string `7243`; the dated preview returned typed ISO dates and fixed-point decimal strings with a continuation cursor.
- Admin readiness and authenticated OpenAPI succeeded. The schema contains 12 paths and 13 operations.

Admin submitted recent refresh job **`c50f856f-f376-4f4e-8ba2-400ddecd5a03`**. The API returned 202/queued; replaying its idempotency key returned the same job. A separately invoked worker consumed it and reread the live EIA source with the upstream key confined to that worker process.

| Observed result | Value |
| --- | --- |
| Job final status | `succeeded` |
| New raw snapshot | `08b5b322bfde46ea882f030d1ef49f31` |
| New model, version 2 | `9a42d81a44854eccbfc7a82141225466` |
| Prior model retained | `9811a42ac9a34b4581d00a9bbd5d5d9b` |
| Live requests / retries | 9 / 0 |
| National / facility / generator rows | 7,243 / 438,119 / 714,657 |
| Added / changed / deleted observations | 0 / 0 / 0, in every route |
| Complete ingest through joint publication | 244.819 seconds on this machine |

All three source logical hashes and all ten analytical logical hashes remained unchanged. Status polling identified the new matching source/model pair. After refresh, Viewer still received only two datasets and its national count request missed the prior cache generation. Complete current-model verification passed: ten datasets, twelve FKs, physical/logical hashes, schemas, source binding and metric checks. The temporary acceptance server was stopped after verification; the README command starts it again.

## Automated and packaging acceptance

| Check | Observed outcome |
| --- | --- |
| Python 3.14.2 full suite | **614 passed, 1 skipped**, 212.16 seconds |
| Fresh clone + new Python 3.12.14 environment | **614 passed, 1 skipped**, 209.34 seconds |
| Final OpenAPI annotation follow-up in fresh clone | **12 passed**, 2.96 seconds |
| Ruff lint / format | Passed; 74 source/test files formatted |
| Dependency, Django system and migration checks | Passed; no pending model migration |
| OpenAPI standards | `openapi-spec-validator 0.9.0` accepts 3.1.0; `jsonschema 4.26.0` checks 35 schemas and 18 examples |
| npm audit | Zero reported advisories in the pinned npm graph at audit time |

The only skip is the existing Windows symlink-creation privilege test; it is not counted as a successful symlink-boundary test. Tests directly exercise engine host-file/other-Parquet/COPY/ATTACH denial, an HTTP receiver receiving zero requests, actual oversized allocation rejection, expensive-query timeout and recovery, compiler isolation/no fallback, process-shared admission and real killed-refresh-worker recovery.

The fresh clone initially contained no key, data, local service state, virtual environment or npm modules. Locked Python/npm installation succeeded. The full suite ran at functional commit `8688ffc02945cd1e85acbca9c9cb61bd288abdf9`; the clone then advanced to `9a1b391e2a90ed1417e34fb839b7cf29240e68ae` for final documentation/OpenAPI annotation changes and reran the twelve affected contract tests plus Ruff/migration checks. Both states had clean Git trees. No real credentials or datasets were copied into the checkout.

A wheel built from `9a1b391` was installed with exactly seven pinned runtime dependencies into a new Python 3.14 environment. From an external working directory using `python -I`, all 50 package files matched committed source. The compiler subprocess, WASM query and authenticated installed Django API succeeded without an editable source import, EIA key or the project's private configuration. Two SQL assets/18 report queries, frozen v1 contract, OpenAPI and all runtime assets/license/provenance loaded from the installed package. Exact date/decimal results survived. Observed compiler/standalone engine/authenticated-query times were 0.328/1.900/2.242 seconds, respectively; these are individual measurements, not load guarantees.

The wheel SHA-256 is `919d71a4e9c906100ffd957417844627a10288923f82a2b66cebf13b6bed84e7`. Three source-tree CRLF versus committed LF differences were recorded as newline-only; installed wheel bytes match the committed files.

## Credential audit and reproducibility

Actual EIA and generated Django/demo secret values were checked without printing them. The audit passed across five values, all 243 Git objects then present including unreachable objects, 114 tracked files, 18 persisted data JSON files, 66 work-root files including logs/reports, service/catalog SQLite bytes, and 52 Parquet files with 216 decompressed VARCHAR columns. Credential files were ignored and untracked. The generated credential file had inheritance disabled and one explicit access rule for the current Windows user. These counts identify that audit's scope, not future edits.

README contains the complete setup and session/CSRF examples. In an already initialized local checkout:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer.web.local serve --port 8000
# Separate terminal, with EIA_API_KEY supplied only when refresh is needed:
.\.venv\Scripts\python.exe -m outage_explorer.web.local worker
```

Machine evidence is retained in ignored `work/`: `part3-live-before.json`, `part3-live-after.json`, `part3-worker.json`, `part3-worker.log`, `part3-http.log`, `part3-publication-evidence.json`, `part3-current-verify.json`, `part3-py314-tests.log`, `part3-clean-checkout-evidence.json`, `part3-wheel-acceptance.json`, `part3-secret-audit.json` and `part3-credentials-acl.json`. Committed source, contracts and tests provide the repeatable mechanism; private credentials, generated state and downloaded analytical data are excluded from Git.

## Explicit limits

Windows is the live-verified runtime/resource platform. POSIX allocation controls and Windows/Linux CI configuration are present; remote CI and Linux runtime acceptance are not asserted. Hosted HTTPS/proxy/host configuration, PostgreSQL, off-machine backup and Azure operations remain deployment work. No OAuth/OIDC, row/column-level tenancy or JOIN/CTE/subquery language support is claimed. The local API is not Part 4's web UI.

The compiler, runtime packages, shim and host publication catalog are trusted. The Python compiler is resource-isolated rather than filesystem-jailed; the engine's WASM import capabilities provide its filesystem/network boundary. A host administrator is outside product-persona isolation. Selected Parquet input is limited to 64 MiB; much larger datasets need measured partitioning/execution work. A refresh interrupted after data commit may have published despite an interrupted job; its status explicitly requires publication review.
