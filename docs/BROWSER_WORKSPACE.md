# How the browser workspace keeps an investigation consistent

Outage Explorer turns published EIA observations into a focused workspace for following the U.S. nuclear fleet. Viewer follows national trends; Analyst explores all analytical datasets and writes SQL; Admin also manages product accounts and refreshes the publication. Analytical retrieval uses the authenticated Django API; Explore can also narrow its already loaded page in memory. The browser never opens Parquet files or runs a second analytical engine.

The central product choice is to keep the chart, table and SQL on **one immutable publication** while an investigation is in progress. A refresh can finish in the background, but the user explicitly chooses **Use latest** before moving to its results. This makes empty results, errors, delayed responses and new data easier to interpret: old rows must not masquerade as a newly answered question.

Read [PROJECT_TOUR.md](PROJECT_TOUR.md) for the basic system and [LOCAL_OPERATIONS.md](LOCAL_OPERATIONS.md) for setup. This guide moves from the user tasks to state/lifecycle rules, implementation, tradeoffs and practice. The exact [API contract](API_CONTRACT.md), [query security boundaries](QUERY_SECURITY.md) and dated [verification record](NOTES.md) provide the references behind it.

## Five views with server-owned permissions

| View | Task and behavior |
| --- | --- |
| Fleet overview | Follow the stored US-08 metric, its same-day MW numerator/denominator and observation date; select up to 366 days; inspect an SVG chart and exact table |
| Explore data | Build typed dataset queries, immediately filter loaded rows, sort/page through applied backend results and export visible rows; inspect schema/units separately |
| SQL workspace | Select one permitted source, enter bounded SELECT SQL, insert schema identifiers, inspect errors/results/pages and reuse recent session queries |
| Users | Admin creates accounts, edits names/access, generates replacement passwords and deletes accounts with explicit confirmation |
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

### Query the dataset and filter the loaded page

The compact command bar keeps dataset selection and **Apply query** visible while a collapsible editor holds typed field/operator/value conditions and ordering. Date scope and entity IDs use the same condition rows, avoiding duplicate date controls or an incomplete ID selector. The initial date condition covers the latest available 30 days where supported. Conditions AND together; exact values and type-compatible operators are validated before a request.

**Filter loaded rows** responds immediately to typing, paste and cut, without an API call. Plain Enter does not submit the query. It searches canonical cell values case-insensitively and literally: textual IDs, ISO dates, exact unformatted numbers and booleans; nulls supply no searchable text. Only one bounded raw page and its search index are retained. Matching rows are shown without changing the server's cursor or ordering; the count explicitly distinguishes visible rows from loaded rows. A local-empty result explains that Apply can search beyond this page.

Apply captures the current text, typed conditions, ordering, page size and immutable model, and queries the full dataset. Next/Previous and header sorting use that **last applied** database scope even while local text or conditions have pending edits. The server supplies signed seek tokens; the browser retains at most 50 previous page positions and never manufactures offsets. Every response is checked against its captured generation, dataset and model. The latest local text filters a newly arriving page without overwriting that text or promoting it into the earlier request.

**Clear filter** restores the loaded raw page; **Reset query** clears draft clauses/text and restores primary-key ordering. Neither sends a request or silently clears an applied database search: Apply is required. The quiet unapplied indicator and applied-query summary keep these scopes visible. **Export visible rows** uses the locally visible subset and its loaded dataset/model, retaining server order and exact values. SQL types remain available through header titles and the schema/units disclosure rather than occupying a second primary-grid header line.

### Run and refine SQL

The client captures the selected dataset, SQL, caps and model on Run, and rejects input over 16 KiB. UI controls offer retained-row caps of 100/500/1,000 and page sizes of 25/50/100. The backend remains responsible for SQL policy, permissions, result caps and execution isolation; bypassing client checks cannot expand them.

Each authorized dataset has three **Query starters** covering browsing, filtering and summaries. Loading a starter updates the draft without executing SQL or adding a recent-history entry. Recent queries remain the actual session's executions. The 30 templates are checked against the current SQL policy and synthetic model data.

The self-hosted CodeMirror editor provides SQL token colors, line numbers, bracket matching/closing, undo, and local suggestions for the selected dataset's fields and the supported SQL vocabulary. Typing opens suggestions; `Ctrl+Space` requests them explicitly, Enter accepts a suggestion, Escape dismisses it, and Tab moves to the next control. `Ctrl+Enter`/`Cmd+Enter` runs SQL. Clicking a schema field inserts its quoted name at the retained cursor or selection. Dataset switches reset editor undo history so another dataset's draft is not revealed by Undo. No query text is sent to an external completion service.

These helpers do not expand the backend's single-dataset SELECT language or perform semantic authorization. The backend remains authoritative. If the enhanced editor asset cannot load, a labeled plain-text editor still supports query execution and schema insertion. CodeMirror's generated styles use a fresh response-specific CSS nonce; inline scripts and unrestricted inline styles remain blocked.

During selection replacement, Chromium can try to copy a highlighted token's color/weight into an inline style attribute. CSP blocks that native style copy; editing and completion continue, with highlighting supplied by the approved style sheets. Browser checks distinguish this known blocked native operation from application script/style-sheet failures.

The editor source is [sql_editor.js](../src/outage_explorer/frontend/editor/sql_editor.js). After changing it, run `npm run build:editor`; `npm run check:editor` verifies the checked-in bundle and dependency licenses, including in CI. Normal installation and Docker use the packaged bundle without requiring an editor build or external CDN.

Result cursors and generation/model checks keep pages attached to the executed query. Changing draft SQL or limits disables paging incompatible with the displayed result and identifies it as a previous run. At most ten query texts/statuses/timestamps are retained in the current scoped session history; no result rows or authentication fields are stored there. Retained SQL text is not scrubbed for sensitive literals.

### Save custom tables and graphs

Analysts and Admins can run a query, optionally choose its time/value/series fields under **Graph over time**, then **Save to overview** with a name. Loading a starter does not execute or save it. The saved definition is private by default; explicit audience choices share the definition, including SQL text. Every recipient must independently have access to its dataset. Viewers consume permitted shared views; saving/sharing does not change the existing direct SQL policy.

**Custom tables and graphs** appears beneath the built-in Overview. Load a named view to execute it against the workspace's adopted publication, inspect its graph and paged exact table, or export the visible table page. Owners can rename it, change chart/audience settings, open its SQL for editing and update the definition, or confirm deletion. Definitions survive reloads and container recreation; analytical rows are not stored with them. A definition's creation model is provenance, while subsequent executions follow the reader's adopted publication.

Charts collect the complete retained SQL result (up to 1,000 rows and an 8 MiB client aggregate budget), not just the visible page. A capped result is clearly labeled and is not claimed to cover the full dataset. Choose a date/timestamp and numeric output; repeated times require a series field, with at most eight series. Duplicate time/series pairs require aggregation in SQL. Nulls and missing daily observations break lines; display coordinates are approximate and tables retain exact values. Queries without a compatible time axis remain useful saved tables.

Saved-view metadata is paged, with at most 50 definitions per owner and eight loaded previews retained in memory. Loading is explicit so opening Overview does not launch every saved SQL query. The API rechecks ownership/audience, dataset permissions, session and definition revision; concurrent changes return a conflict instead of silently overwriting settings.

### Manage product accounts as Admin

Open **Users** under Administration to search the account directory or page through its bounded results. **Create user** collects username, optional first/last names, product role and active state. The server creates a random password and displays it once in a dedicated credentials dialog. Passwords are initially masked. Copy or retain the credential privately before closing or switching away; displayed credentials are cleared on dismissal, navigation, visibility loss and session disposal. A reply arriving in an already-background tab is held only in private memory for at most five minutes and displayed masked on return, with a fresh session check before displaying another account's credential. A clipboard failure leaves a selectable credential instead of claiming that copying succeeded.

**Edit user** changes profile fields, role or active state. Names and usernames do not reset a password. Role/active-state changes revoke existing sessions, including when a later edit restores the old value. **Reset password** explicitly replaces the password and displays a new one-time credential. Resetting the current Admin's own password ends that session after acknowledgment, so the new password must be retained first. None of these operations updates the initialization passwords in the private local configuration.

**Delete user** requires confirmation identifying the account and explains that saved views and cached results will be removed. Refresh history remains. The server rejects self-deletion, self-deactivation, self-demotion, removal of the last active Admin and deletion while the account owns queued/running refresh work. Infrastructure staff/superuser accounts are outside this directory. Concurrent account edits return a version conflict and require a fresh record rather than silently overwriting each other. These controls remain Admin-only even if browser markup is modified; see the [account API](API_CONTRACT.md#admin-account-management).

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

Dynamic values become text nodes/`textContent`, never data-driven HTML interpolation. Templates escape identity and use inert `json_script` bootstrap. External text resembling markup therefore stays text. Column label lookup uses own-property checks so valid aliases such as `constructor` or `toString` are handled as names rather than inherited properties. Private HTML/API responses use `no-store`; HTML restricts scripts/connections to self and permits the editor's generated style sheets through a fresh response nonce. Inline scripts, eval and arbitrary inline styles remain blocked.

CSV export includes the currently displayed page only, narrowed to visible rows in Explore when its local filter is active, with a dataset/model-prefix filename. Every cell is quoted and embedded quotes escaped; original scalar text is retained. Formula-like text such as `=SUM(...)`, leading tabs/line breaks and zero-padded text identifiers such as `001` receive an apostrophe guard, including headers. There is no undisclosed bulk download or background query. Spreadsheet applications still choose import types, so CSV cannot guarantee every inferred identifier or large integer survives their import; canonical programmatic consumers should use the typed API.

### Make the bounded interface usable without a pointer

The UI uses native labeled controls, visible focus, semantic tables with captions and column scope, explicit loading/empty/error/not-allowed states, live messages and `aria-busy`. Skip links and landmarks support navigation. Hash navigation moves focus to the view heading; Escape closes mobile navigation and returns focus.

Tables scroll inside their containers on narrow screens. The layout supports reduced motion and forced colors. The SVG includes title/description and an exact table alternative. Its hover tooltip is pointer-only and aria-hidden; keyboard users have the table. This is a deliberate alternative, not a claim that every plotted point has an interactive keyboard tooltip.

Automated scans, keyboard checks and visual review cover basics. They do not replace actual analyst or assistive-technology user testing or establish full WCAG certification.

## Why these product choices fit this version

| Choice | Reason and cost |
| --- | --- |
| Native modules rather than React | Five focused views use native modules and manual DOM/lifecycle management. The specialized CodeMirror editor has one reproducible local bundle; the surrounding UI needs no framework build. That keeps the runtime surface small, with manual race management as a maintenance cost. A framework becomes useful with more reusable components, navigation/collaboration complexity or team needs. |
| Server role policy plus omitted Admin markup | The interface shows truthful capabilities while the server enforces them. Removing markup restrictions must still fail at the API. |
| Pinned model rather than automatic freshness | Chart, preview and SQL answer one reproducible investigation. New data is visible and explicitly adopted. Older versions need backend retention/expiry policy, and users can deliberately remain on old data. |
| Bounded native tables rather than a virtualized grid | Typical pages are 25–100 rows and schemas are small; native semantics help keyboard/screen-reader use. Very wide or much larger pages would make DOM work costly and need another design. |
| Small custom SVG chart rather than a charting library | One line/area metric needs no extra dependency. Axes, responsiveness, tooltip and accessibility are maintained manually; a richer chart suite could justify a library. |
| Session history plus named saved views | Recent text stays scoped to the tab/session; deliberately saved query/chart definitions persist in application state with owner and audience checks. Result rows are not saved with definitions. |
| Indeterminate refresh rather than a progress percentage | Lifecycle state is known; a reliable work denominator is not. A future percentage would need explicit stage/work counters and defined semantics. |
| Guarded current-page CSV | Users export exactly what they see with version context and spreadsheet guards. Whole-dataset exports are absent, and receiving spreadsheet typing is outside application control. |

The added stories are consistency during refresh, recovery from invalid/empty/busy results without relabeling old rows, and export with reproducible dataset/model context. Deliberate exclusions are a dashboard builder, multi-table SQL, collaborative investigations and cloud deployment. Next priorities are hosted runtime/operator-recovery validation, larger-volume measurements, real analyst and assistive-technology testing, then saved investigations and richer filtering informed by that feedback. These remain future work.

## Implementation map and a practical reading order

The Python package ships templates, native scripts, CSS, SVG and the prebuilt CodeMirror editor bundle/licenses. esbuild is a development dependency used only to rebuild or verify that editor asset. Normal setup and Docker use the checked-in bundle; the browser downloads no CDN code or external fonts/scripts. Public assets come from an explicit filename allowlist with packaged-resource lookup, correct MIME types, nosniff and SHA-256 ETag revalidation under DEBUG false. Unknown names are rejected before resource lookup. Public asset caching is separate from private HTML/API no-store.

| File | Responsibility |
| --- | --- |
| [frontend/views.py](../src/outage_explorer/frontend/views.py), [urls.py](../src/outage_explorer/frontend/urls.py), [templates](../src/outage_explorer/frontend/templates/outage_explorer) | GET-only routes, persisted identity before/after rendering, identity-only bootstrap, protected/public asset boundary |
| [app.js](../src/outage_explorer/frontend/static/outage_explorer/app.js) | Workspace composition, navigation/focus, session lifecycle, catalog model pin and publication adoption |
| [api.js](../src/outage_explorer/frontend/static/outage_explorer/api.js) | Same-origin JSON/CSRF/errors, 22-second transport, tab analytical queue and response identity/shape checks |
| [overview.js](../src/outage_explorer/frontend/static/outage_explorer/overview.js), [chart.js](../src/outage_explorer/frontend/static/outage_explorer/chart.js) | Stored metric, period selection, gap/null/zero semantics and native SVG |
| [explore.js](../src/outage_explorer/frontend/static/outage_explorer/explore.js) | Dataset/schema/default filters, applied-versus-draft state and pinned keyset pages |
| [sql.js](../src/outage_explorer/frontend/static/outage_explorer/sql.js) | Executed-versus-draft SQL, caps/paging, identifier insertion and scoped history |
| [users.js](../src/outage_explorer/frontend/static/outage_explorer/users.js) | Admin account directory, versioned edits/deletion and transient one-time credentials |
| [refresh.js](../src/outage_explorer/frontend/static/outage_explorer/refresh.js) | Mode-bound retry key, owned-job polling, generation guards and outcomes |
| [table.js](../src/outage_explorer/frontend/static/outage_explorer/table.js), [dom.js](../src/outage_explorer/frontend/static/outage_explorer/dom.js) | Safe text, exact scalar/date formatting, state messages and guarded page export |
| [login.js](../src/outage_explorer/frontend/static/outage_explorer/login.js) | CSRF bootstrap, JSON login and safe session transition |

The implementation path runs from server shell/bootstrap to app composition and transport, then the overview, Explore, SQL, user-management and refresh controllers. Their regression tests establish state ownership, identity/model binding and behavior when responses arrive out of order.

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
