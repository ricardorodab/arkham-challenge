# How the browser workspace keeps an investigation consistent

Outage Explorer turns published EIA observations into a focused workspace for following the U.S. nuclear fleet. Viewer follows national trends; Analyst explores all analytical datasets and writes SQL; Admin also refreshes the publication. The browser uses the authenticated Django API for every data operation. It never opens Parquet files or runs a second analytical engine.

The central product choice is to keep the chart, table and SQL on **one immutable publication** while an investigation is in progress. A refresh can finish in the background, but the user explicitly chooses **Use latest** before moving to its results. This makes empty results, errors, delayed responses and new data easier to interpret: old rows must not masquerade as a newly answered question.

Read [PROJECT_TOUR.md](PROJECT_TOUR.md) for the basic system and [LOCAL_OPERATIONS.md](LOCAL_OPERATIONS.md) for setup. This guide moves from the user tasks to state/lifecycle rules, implementation, tradeoffs and practice. The exact [API contract](API_CONTRACT.md), [query security boundaries](QUERY_SECURITY.md) and dated [verification record](NOTES.md) provide the references behind it.

## Four views, one authorized catalog

| View | Task and behavior |
| --- | --- |
| Fleet overview | Follow the stored US-08 metric, its same-day MW numerator/denominator and observation date; select up to 366 days; inspect an SVG chart and exact table |
| Explore data | Choose a permitted dataset, inspect schema/units, apply supported date/facility filters, page through backend results and export the current page |
| SQL workspace | Select one permitted source, enter bounded SELECT SQL, insert schema identifiers, inspect errors/results/pages and reuse recent session queries |
| Data refresh | Admin queues work, sees only owned job history, follows explicit queued/running/outcome states and chooses whether to adopt a new publication |

Viewer sees exactly `national_daily` and `fleet_daily`. Analyst and Admin see all ten analytical datasets. Admin navigation and pane markup are omitted from other personas' HTML. Dataset choices and schema suggestions come from the authorized catalog rather than a second client-side role matrix. The server still authorizes every request; editing JavaScript or revealing hidden controls grants no access.

`GET /login/` is public. `GET /` requires a current unexpired session and valid persona. Both HTML routes are GET-only and ignore caller-supplied return URLs. Forms have safe same-origin POST actions so secrets do not enter a GET URL, but the application requires JavaScript: direct POST to the HTML login route returns 405 rather than implementing a complete non-JavaScript login fallback. The login page contains no shipped credentials or default passwords.

## Trace the main user flows

### Sign in and open the workspace

1. The public login page serves a labeled form. `login.js` first gets the CSRF bootstrap token and enables submission only after success.
2. It submits username/password in same-origin JSON to `/api/v1/auth/login`. Successful login rotates the in-memory CSRF token, blanks the password, clears old SQL history, broadcasts the signed-in event and replaces the location with `/`.
3. Authentication errors are announced. A 401 blanks and refocuses the password field.
4. Django renders the private page with identity/session bootstrap only. It rechecks persisted identity after rendering; it never reads analytical rows to build the HTML.
5. `Workspace.init` bootstraps CSRF, fetches the authorized catalog and configures all views with its model ID. Hash navigation changes views without a full reload.

The session cookie is HttpOnly; the CSRF cookie is separate. The in-memory token accompanies JSON mutations; the browser retries an explicit CSRF rejection only once after refreshing that token. An authentication failure or changed permission is distinguished from a transport/network failure. The server remains the authentication authority. The [API authentication sequence](API_CONTRACT.md#browser-session-and-csrf) gives the exact protocol.

### Read the national overview

The overview requests `fleet_daily` pinned to the catalog model, with a maximum inclusive period of 366 days and a backend page limit of 1,000. A continuation cursor is an error here: it would mean a partial period, so the chart must not silently render it as complete.

Cards show the latest observation **within the selected period**, its date and the MW operands. They do not claim live plant conditions. The chart consumes stored `offline_pct`; it does not reimplement the authoritative metric. [The data guide](DATA_PIPELINE_AND_MODEL.md) defines the same-day national arithmetic and exact rounding.

True zero remains zero. Missing dates and undefined/null ratios split the line instead of becoming zero or being interpolated. Dates are observation calendar dates, formatted with UTC calendar logic rather than converted to the browser's local time zone. Exact values remain in the chart's table and current-page CSV.

### Apply filters and page through data

Selecting a dataset configures controls from its schema. Date/facility filters appear only when supported; the default date window is the latest 30 days. Applying filters captures the dataset, filters, page size and model as the question being answered.

The server supplies opaque keyset cursors. Next uses the returned token; Previous uses the retained cursor chain. The browser does not manufacture SQL offsets. Every result is checked against the expected dataset and model before rendering.

Editing draft controls marks the displayed result as belonging to the **last applied** question and disables incompatible continuation. Export uses that displayed packet and its applied dataset, not whatever the controls currently contain. This is how the product can show an old answer honestly while a user edits a new question.

The facility lookup currently reads one `dim_facility` page with `limit=1000` and ignores its continuation. Present nuclear facilities fit; a future larger domain needs paginated lookup to avoid a truncated selector.

### Run and refine SQL

The client captures the selected dataset, SQL, caps and model on Run, and rejects input over 16 KiB. UI controls offer retained-row caps of 100/500/1,000 and page sizes of 25/50/100. The backend remains responsible for SQL policy, permissions, result caps and execution isolation; bypassing client checks cannot expand them.

The supported language matches the backend's single-dataset SELECT subset. Schema insertion doubles embedded quotes and inserts the selected dataset or column as a quoted identifier. It is identifier assistance, not a SQL parser or semantic completion engine. `Ctrl+Enter`/`Cmd+Enter` runs SQL; `Ctrl+Space` focuses completion. Clicking a schema column also inserts it.

Result cursors and generation/model checks keep pages attached to the executed query. Changing draft SQL or limits disables paging incompatible with the displayed result and identifies it as a previous run. At most ten query texts/statuses/timestamps are retained in the current scoped session history; no result rows or authentication fields are stored there. Retained SQL text is not scrubbed for sensitive literals.

### Ask Admin to refresh data

Admin enqueue creates a `crypto.randomUUID` idempotency key. On an uncertain network outcome the controller keeps that key for a same-mode retry, so a lost response does not require blindly creating another job. Server constraints, not a disabled button, enforce idempotency and global job capacity.

The interface names queued, running, succeeded, failed and interrupted states. Queued/running progress is indeterminate because the backend has no reliable total-work denominator; no percentage is invented. Active jobs poll every two seconds while visible and eight seconds while hidden. Job-ID/generation checks prevent an old history or poll reply from overwriting a newly queued job.

A separate worker consumes jobs. On success, the browser checks for a new model and announces it. The active investigation stays pinned until **Use latest** reconfigures the views and invalidates old pending render work. Failure and interruption retain explicit outcomes; an interrupted job can have published data before its completion record was written. See [refresh transaction boundaries](QUERY_SECURITY.md#refresh-has-three-different-concurrency-controls).

## Prevent old responses from replacing a new answer

Every view captures a request generation and model ID, then compares them before rendering. The shared `AnalyticalQueue` checks that queued work is still current before executing and again after completion. A failed task does not poison later queued tasks.

For example, a request for generators may start, then the user switches to facilities. If the generator response arrives last, it is discarded rather than briefly displayed. The browser regression observes table mutations with a canary, so it checks more than the final table state.

The queue is **per workspace/tab**. Multiple tabs can overlap requests; only server admission coordinates the account globally. For bounded 429 capacity replies, the queue can make up to three attempts when Retry-After is at most five seconds, rechecking current state after each wait. It does not silently retry generic query errors.

Stale requests are normally discarded rather than canceled on filter/dataset changes. A subsequent task can therefore wait behind obsolete work until it finishes or reaches the transport's 22-second timeout. Aborting fetch could improve latency, but it would not prove the server stopped work; generation/model checks are still necessary.

`Workspace.checkLatest` identifies a candidate publication separately from the active model. `applyLatest` configures all controllers and invalidates their generations. Preview and SQL continuations retain their model and applied filters/query. The matching backend model/cursor rules prevent refresh from silently mixing versions across the workspace.

## Keep private state tied to the actual server session

Escaped bootstrap data carries username, persona and an opaque `session_scope`, also returned by the session API. It is an HMAC over the server actor binding, not a raw session identifier, authentication hash or bearer credential. A new session for the same username/persona still has a different scope; a data refresh leaves it stable.

Initialization retires other query-history scopes while preserving current-session reload continuity. Successful login, logout disposal and detected revocation clear old history. `sessionStorage` is tab-session storage, not a durable cross-device saved-query store.

BroadcastChannel expedites cross-tab changes. Visible pages also check every 60 seconds and revalidate on focus, visibility return and persisted `pageshow` restoration. A focus/restore check hides protected content before awaiting confirmation. If a background check is already running, the focus event escalates it to the protective hidden state rather than leaving data visible.

A network failure during protective confirmation leaves a retry shield; it is not interpreted as confirmed logout. Logout clears controller results, drafts/history, timers and catalog and hides content **before** its request. If confirmation fails, the workspace stays hidden with a retry action and does not falsely claim server logout succeeded. Confirmed logout broadcasts to other tabs.

These guards reduce stale private state, but periodic checks are not instant server push and `no-store` is not a universal guarantee against browser page restoration. Already delivered information, downloaded exports and privileged browser/host inspection cannot be remotely erased. The original lifecycle guidance included [MDN pageshow and back-forward cache restoration](https://developer.mozilla.org/en-US/docs/Web/API/Window/pageshow_event); no new external-source verification is claimed. Each new API request still needs current server authorization. The [security guide](QUERY_SECURITY.md) owns that boundary.

## Exact values, safe rendering and accessibility

### Preserve the stored value, then format its presentation

Exact decimal and 64-bit integer strings remain strings in tables and CSV. For example, `9007199254740993` must remain text through serialization, response validation, table rendering and export because Number conversion would lose its exact integer value. `scalarLabel` formats them with string operations, removing presentation grouping/trailing-zero clutter without changing the stored value. Cell titles retain original scalar text. Null has an explicit unavailable label rather than a fabricated zero.

ISO dates are validated by round-trip and formatted using UTC calendar arithmetic to avoid timezone/DST shifts. Cards/chart coordinates convert to JavaScript Number for presentation; the chart tooltip formats up to three fractional digits. The exact table remains authoritative. JavaScript cannot preserve every database decimal by simply converting it to Number.

`verifyPage` validates dataset/model identity, bounded matrix shape, named/typed columns and finite primitive scalars. It accepts numbers and strings; it is not exhaustive semantic schema validation and cannot recover precision already lost upstream. Exactness relies on the backend's typed serialization contract.

### Render text and export only the page shown

Dynamic values become text nodes/`textContent`, never data-driven HTML interpolation. Templates escape identity and use inert `json_script` bootstrap. External text resembling markup therefore stays text. Column label lookup uses own-property checks so valid aliases such as `constructor` or `toString` are handled as names rather than inherited properties. Private HTML/API responses use `no-store`; HTML has a self-only script/style/connect policy without inline/eval allowances.

CSV export includes the currently displayed page only, with a dataset/model-prefix filename. Every cell is quoted and embedded quotes escaped; original scalar text is retained. Formula-like text such as `=SUM(...)`, leading tabs/line breaks and zero-padded text identifiers such as `001` receive an apostrophe guard, including headers. There is no undisclosed bulk download or background query. Spreadsheet applications still choose import types, so CSV cannot guarantee every inferred identifier or large integer survives their import; canonical programmatic consumers should use the typed API.

### Make the bounded interface usable without a pointer

The UI uses native labeled controls, visible focus, semantic tables with captions and column scope, explicit loading/empty/error/not-allowed states, live messages and `aria-busy`. Skip links and landmarks support navigation. Hash navigation moves focus to the view heading; Escape closes mobile navigation and returns focus.

Tables scroll inside their containers on narrow screens. The layout supports reduced motion and forced colors. The SVG includes title/description and an exact table alternative. Its hover tooltip is pointer-only and aria-hidden; keyboard users have the table. This is a deliberate alternative, not a claim that every plotted point has an interactive keyboard tooltip.

Automated scans, keyboard checks and visual review cover basics. They do not replace actual analyst or assistive-technology user testing or establish full WCAG certification.

## Why these product choices fit this version

| Choice | Reason and cost |
| --- | --- |
| Native modules rather than React | Four focused views, bounded pages and limited shared state avoid framework/build overhead and support a small self-only CSP surface. Manual DOM, lifecycle and race management are real maintenance costs. A framework becomes useful with more reusable components, navigation/collaboration complexity or team needs; frameworks are not inherently slower or less secure. |
| Server role policy plus omitted Admin markup | The interface shows truthful capabilities while the server enforces them. Removing markup restrictions must still fail at the API. |
| Pinned model rather than automatic freshness | Chart, preview and SQL answer one reproducible investigation. New data is visible and explicitly adopted. Older versions need backend retention/expiry policy, and users can deliberately remain on old data. |
| Bounded native tables rather than a virtualized grid | Typical pages are 25–100 rows and schemas are small; native semantics help keyboard/screen-reader use. Very wide or much larger pages would make DOM work costly and need another design. |
| Small custom SVG chart rather than a charting library | One line/area metric needs no extra dependency. Axes, responsiveness, tooltip and accessibility are maintained manually; a richer chart suite could justify a library. |
| Session history rather than durable saved queries | Ten recent texts/statuses offer reload continuity with limited retention and no result storage. History is per tab/server scope and does not persist across sessions/devices. |
| Indeterminate refresh rather than a progress percentage | Lifecycle state is known; a reliable work denominator is not. A future percentage would need explicit stage/work counters and defined semantics. |
| Guarded current-page CSV | Users export exactly what they see with version context and spreadsheet guards. Whole-dataset exports are absent, and receiving spreadsheet typing is outside application control. |

The added stories are consistency during refresh, recovery from invalid/empty/busy results without relabeling old rows, and export with reproducible dataset/model context. Deliberate exclusions are a dashboard builder, multi-table SQL, cross-session saved queries/investigations and cloud deployment. Next priorities are hosted runtime/operator-recovery validation, larger-volume measurements, real analyst and assistive-technology testing, then saved investigations and richer filtering informed by that feedback. These remain future work.

## Implementation map and a practical reading order

The Python package ships templates, scripts, CSS and SVG assets. There is no React, bundler, CDN, external font/script or frontend build step. Public assets come from an explicit filename allowlist with packaged-resource lookup, correct MIME types, nosniff and SHA-256 ETag revalidation under DEBUG false. Unknown names are rejected before resource lookup. Public asset caching is separate from private HTML/API no-store.

| File | Responsibility |
| --- | --- |
| [frontend/views.py](../src/outage_explorer/frontend/views.py), [urls.py](../src/outage_explorer/frontend/urls.py), [templates](../src/outage_explorer/frontend/templates/outage_explorer) | GET-only routes, persisted identity before/after rendering, identity-only bootstrap, protected/public asset boundary |
| [app.js](../src/outage_explorer/frontend/static/outage_explorer/app.js) | Workspace composition, navigation/focus, session lifecycle, catalog model pin and publication adoption |
| [api.js](../src/outage_explorer/frontend/static/outage_explorer/api.js) | Same-origin JSON/CSRF/errors, 22-second transport, tab analytical queue and response identity/shape checks |
| [overview.js](../src/outage_explorer/frontend/static/outage_explorer/overview.js), [chart.js](../src/outage_explorer/frontend/static/outage_explorer/chart.js) | Stored metric, period selection, gap/null/zero semantics and native SVG |
| [explore.js](../src/outage_explorer/frontend/static/outage_explorer/explore.js) | Dataset/schema/default filters, applied-versus-draft state and pinned keyset pages |
| [sql.js](../src/outage_explorer/frontend/static/outage_explorer/sql.js) | Executed-versus-draft SQL, caps/paging, identifier insertion and scoped history |
| [refresh.js](../src/outage_explorer/frontend/static/outage_explorer/refresh.js) | Mode-bound retry key, owned-job polling, generation guards and outcomes |
| [table.js](../src/outage_explorer/frontend/static/outage_explorer/table.js), [dom.js](../src/outage_explorer/frontend/static/outage_explorer/dom.js) | Safe text, exact scalar/date formatting, state messages and guarded page export |
| [login.js](../src/outage_explorer/frontend/static/outage_explorer/login.js) | CSRF bootstrap, JSON login and safe session transition |

The implementation path runs from server shell/bootstrap to app composition and transport, then the overview, Explore, SQL and refresh controllers. Their regression tests establish state ownership, identity/model binding and behavior when responses arrive out of order.

Concrete future maintenance constraints include the first-page-only facility lookup, tab queue/cancellation latency, generic response checks relying on the trusted API contract, and custom rendering/large stylesheet cost as scope expands. Firefox/Safari, hosted Linux/Azure and real assistive-technology acceptance have no current recorded proof.

## How the interface has been checked

Verification methods, actual corrections and scoped results are summarized in [docs/NOTES.md](NOTES.md). No new browser, Python or JavaScript run is claimed here. Root `node_modules` is currently absent after the authorized cleanup; restore dependencies using the [operations guide](LOCAL_OPERATIONS.md) before future analytical/runtime checks.

The November 2 frontend review recorded 17 JavaScript passes, repeated during the November 3 documentation cleanup. The cases cover DST/leap dates, null/zero/decimal/large-integer formatting, prototype-like aliases, CSV formulas/IDs/headers, chart gaps/outliers, bounded history, examples/identifier quoting, queue races, transport/CSRF rotation/session-loss versus network errors, focus escalation, history-scope retirement/same-account replacement and delayed refresh history.

| Test source | What to study |
| --- | --- |
| [Client logic](../tests/frontend/logic.test.mjs) | Date/precision, guarded CSV/chart gaps, stale queue, transport/CSRF/session errors, protective session checks/scope replacement and refresh history races |
| [Frontend routes](../tests/test_frontend_routes.py) | Analytical access deliberately fails if attempted during HTML rendering; fresh role/scope; data-refresh versus session independence; escaping and revocation during render; asset ETag/traversal allowlist |
| [OpenAPI](../tests/test_openapi.py), [session revocation](../tests/test_session_revocation.py) | Route/API contract and server-side session enforcement |
| [Browser authentication](../tests/browser/auth.spec.cjs) | Personas, protected navigation/reload and cross-tab revocation |
| [Browser analytics](../tests/browser/analytics.spec.cjs) | Real filtering/keyset pages/current-page CSV and real isolated SQL success/write rejection |
| [Browser states](../tests/browser/states.spec.cjs) | Controlled loading, 403/429/retry and stale-response interception; intermediate DOM canary |
| [Browser refresh](../tests/browser/refresh.spec.cjs) | Separate synthetic workers, success/failure, preserved old publication and explicit adoption |
| [Browser accessibility](../tests/browser/accessibility.spec.cjs) | Ten desktop/mobile axe scans, focus and 320px metric reflow |

Browser configuration defines 18 Chromium cases, one worker and zero automatic retries. Its fresh synthetic data/state and DEBUG-false Waitress server on port 8766 are separate from operator accounts/data and live EIA. Success paths use the real compiler/WASM and separate worker; controlled error/stale states alone use interception. [tests/browser/README.md](../tests/browser/README.md) documents the fixture and future output paths. Configuration is not a passing result, and older full-browser/fresh-checkout/wheel/targeted accessibility runs remain historical Chromium-on-Windows evidence. Incomplete axe checks remain visible; zero automated violations are not certification.

For future verification after dependency restoration, install before starting runtime acceptance and keep port 8766 free:

```powershell
npx playwright install chromium
npm run test:unit
npm run test:browser
.\.venv\Scripts\python.exe -m pytest tests/test_frontend_routes.py tests/test_openapi.py tests/test_session_revocation.py
```

Those commands describe future verification, not a newly executed result. The latest recorded full Python suite is still 654 passed, one failed and one skipped, with the timeout assumption unresolved.
