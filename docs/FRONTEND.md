# Browser workspace

Outage Explorer uses Django templates and native JavaScript modules over the existing [JSON API](API.md). The HTML views load identity and session state only. Catalogs, observations, SQL results and refresh jobs come through authorized API requests; browser code never opens analytical files.

## Pages and workspaces

`GET /login/` is public. `GET /` requires a current, unexpired server session and valid product persona. Both HTML routes are GET-only and ignore user-supplied return URLs. Forms have safe same-origin POST fallbacks; JavaScript submits the actual JSON requests. There are no shipped credentials or username/password defaults in the login page. Follow the [README](../README.md) for local account setup.

The protected page contains four views:

| View | Behavior |
| --- | --- |
| Fleet overview | Stored US-08 metric, latest observation values, a selected period up to 366 days, an SVG chart and its exact data table |
| Explore data | Permitted datasets, schema/units, supported date/facility filters, backend keyset pages and current-page export |
| SQL workspace | One permitted source, bounded read-only SQL, result cap/paging, identifier completion and recent session queries |
| Data refresh | Admin-only queue action, owned job history, queued/running/outcome states and explicit use of a new publication |

Viewer receives only the two national datasets. Analyst and Admin receive all ten datasets. Admin navigation and pane markup are omitted from other personas' HTML; the API remains the permission authority for every operation.

## Values and publication consistency

Tables retain decimal/integer strings; grouping/trailing-zero removal changes presentation only, and cell titles retain original scalar text. Cards/chart coordinates use presentation values from the stored metric. Observation dates are displayed in UTC. Nulls remain unavailable; missing dates/undefined ratios break the line rather than becoming zero. See [DATA_MODEL.md](DATA_MODEL.md).

One catalog model ID pins analytical requests across the workspace. Preview and SQL continuations keep that model and their applied filters/query. A newer publication produces a notice; **Use latest** explicitly adopts it and invalidates old pending render work. Changing draft Explore filters or SQL/limits marks existing results as belonging to the previous application/run and disables incompatible continuation.

Analytical requests share a browser queue; the server independently enforces admission/execution limits. Sequence/model checks discard obsolete replies after dataset, filter or publication changes. Refresh requests also use generation guards so delayed history cannot replace a newly queued job.

## Session and interaction behavior

Cookies stay HttpOnly; an in-memory CSRF token accompanies every JSON mutation. Escaped HTML bootstrap data contains username/persona and an opaque `session_scope`, also returned by the session API. It identifies session replacement without exposing a raw session ID/authentication hash. Requests follow [Django's CSRF header pattern](https://docs.djangoproject.com/en/5.2/howto/csrf/).

History retains at most ten query texts/statuses in `sessionStorage`, never result rows. Initialization retires other scopes while preserving current-session reload history. Successful login, logout disposal and detected revocation clear old history. BroadcastChannel coordinates tabs; server scope comparisons supply the fallback.

Focus, visibility return and persisted `pageshow` hide content for revalidation, including during a pending background check; visible pages also check periodically. Failed confirmation leaves a retry shield. Logout clears results before its request; an unknown network outcome stays hidden with a retry action. Server authorization remains primary: these guards do not promise instant remote erasure of displayed information. See [SECURITY.md](SECURITY.md).

Native controls, focus indicators, semantic tables, live states and mobile navigation support keyboard use. `Ctrl+Enter`/`Cmd+Enter` runs SQL; `Ctrl+Space` focuses completion. Insert a suggested column/dataset or click a schema column. Completion quotes identifiers, not arbitrary SQL syntax. The chart has a table alternative; narrow-screen tables scroll within their container.

CSV export includes **only the currently displayed page**, with original scalar text and a dataset/model-prefix filename. Every cell is quoted. Formula-like text, leading tabs/line breaks and zero-padded text identifiers receive an apostrophe guard, including headers. No background request silently exports additional rows. Spreadsheet applications still determine their own import typing.

## Implementation map

The [frontend app](../src/outage_explorer/frontend) serves templates and an explicit allowlist of packaged assets. Public asset responses use ETags/revalidation; private HTML/API responses use `no-store`. There is no browser framework or frontend build step.

| Module | Responsibility |
| --- | --- |
| `app.js` | Session lifecycle, catalog/model pin, navigation and workspace coordination |
| `api.js` | Same-origin JSON/CSRF/errors, serialized analytical queue and reply checks |
| `overview.js`, `chart.js` | Stored fleet metric, period selection and native SVG rendering |
| `explore.js` | Dataset/schema/filter state and pinned preview pages |
| `sql.js` | SQL execution/paging, identifier insertion and private query history |
| `refresh.js` | Admin enqueue/idempotency, owned job polling and outcome rendering |
| `table.js`, `dom.js` | Text-only cells, scalar/date formatting and guarded page export |
| `login.js` | CSRF bootstrap, JSON authentication and safe login transitions |

## Verification

Install dependencies before starting a service/test run, rather than changing runtime files while requests execute. After the README's Python/Node setup, browser installation and checks are:

```powershell
npx playwright install chromium
npm run test:unit
npm run test:browser
.\.venv\Scripts\python.exe -m pytest tests/test_frontend_routes.py tests/test_openapi.py tests/test_session_revocation.py
```

The unit suite covers 17 client logic cases. The browser suite is configured for 18 Chromium cases against an isolated synthetic fixture, including keyboard, permission, state, pagination/export, stale-response, refresh and accessibility scenarios. Its configuration uses one worker and zero automatic retries. Final executed results and platform scope belong in [PART4_ACCEPTANCE.md](../PART4_ACCEPTANCE.md); configuration alone is not a passing result. [PRODUCT.md](../PRODUCT.md) records product choices and deliberate omissions.
