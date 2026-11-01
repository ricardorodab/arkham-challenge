# Part 4 acceptance — browser application

Version **0.4.0** implements the four core browser capabilities and all seven extras on page 6 of the supplied challenge. It uses the existing Django API, real model contracts and isolated query/refresh services. The local delivery does not provision Azure hosting.

## Requirement evidence

| PDF requirement | Implemented behavior | Authoritative evidence |
| --- | --- | --- |
| Login | Labeled login form, same-origin JSON login with CSRF, generic credential errors, no shipped/default passwords | `frontend/templates/outage_explorer/login.html`, `login.js`; Python frontend/API tests; Chromium login/keyboard cases |
| Available datasets | Choices and schema come from the authenticated catalog. Viewer has exactly two national datasets; Analyst/Admin have ten | `app.js`, `explore.js`, `sql.js`; real-data UI metadata, browser permission case and API authorization tests |
| One backend-paginated selected table | Typed table, applied date/facility filters, backend keyset Next/Previous pages, immutable model pin | `explore.js`, `table.js`; real API browser pagination/filter case; narrow-date real-data check |
| SQL input, results and errors | Native editor, bounded selected-dataset SELECT, real sandbox results, exact scalar strings, safe structured errors and capped backend paging | `sql.js`, `api.js`; browser real-SQL/write-rejection case, SQL-empty real-data check, compiler/sandbox/API tests |
| Persistent session/protected routes | Server session survives reload while valid; protected HTML verifies persisted identity before/after rendering; returning tabs shield/revalidate; logout/revocation disposes private state | `frontend/views.py`, `app.js`, shared session scope; Python route/revocation tests, browser reload/cross-tab/restore cases, JS race/scope tests |
| Admin refresh/progress/outcome | Admin-only HTML plus server authorization; durable owned jobs, indeterminate queued/running progress, explicit success/failure, idempotent retry, new-publication notice | `refresh.js`; real separate-worker synthetic E2E success/failure and genuine EIA/UI refresh recorded below |
| Loading, empty, error, not allowed | Separate safe states for workspace requests, SQL and refresh; permission failures never leave old rows presented as a new answer; network uncertainty retains a session retry shield | Browser controlled-state/stale-response/403/429 cases, real-data empty SQL, JS lifecycle tests |
| Autocomplete/history/export/responsive | Authorized identifier suggestions/schema insertion, Ctrl/Cmd+Enter, last ten session-scoped queries, exact current-page guarded CSV, contained mobile tables/navigation | `sql.js`, `dom.js`, CSS; JS CSV/history tests, browser export/completion/history/390px checks and real 320px review |
| US-08 insight/visualization | Stored metric, MW operands/date, selected-period native SVG, preserved zero, broken lines for nulls/missing dates, exact accessible table/CSV alternative | `overview.js`, `chart.js`; browser 10%/0%/20% fixture with calendar gap, JS chart tests, real-data screenshots |
| Tests/accessibility basics | Component logic, protected route/API tests and real backend E2E; labels, focus/keyboard, semantic tables, live state messages, contrast and chart alternative | 17 JS tests, 18 Chromium cases, ten axe scans, Python suite; platform/count details below |
| One-page PRODUCT.md | Added stories, UX decisions, exclusions and next priorities, 372 whitespace-counted words | `PRODUCT.md`; one A4 page at Arial 11pt / 20mm margins, 724px content within 971px usable height; `work/part4-product-onepage-evidence.json` |

The shared persona policy remains the server's authority. Hidden Admin navigation is a usability choice, not the security boundary. Browser state never grants dataset access. SQL permissions, row/output/time limits and process isolation are not relaxed for the UI.

## Product and implementation

The browser uses Django templates and ten native JavaScript modules, system fonts and local SVG/CSS. Assets are packaged and served through a fixed allowlist with correct MIME types and ETag revalidation under `DEBUG=False`; no development asset server, CDN or frontend build is needed. HTML uses self-only script/style/connect CSP without inline/eval allowances. External data is rendered with text nodes rather than HTML interpolation.

One model ID pins overview, exploration and SQL. Draft filter/query changes disable incompatible paging. Request-generation/model checks discard delayed replies; the analytical queue respects the backend's per-user execution budget. A new publication is announced and adopted explicitly through **Use latest**. This prevents a refresh from silently mixing an active investigation's versions.

History stores query text/status only, scoped by the actual opaque server-session binding; it retires old scopes and stores no results or credentials. Focus/visibility/bfcache restore revalidates before revealing protected data, including escalation of an already-running periodic check. Network failure does not falsely establish logout. These controls do not promise remote erasure of already observed/exported information.

CSV contains the displayed page only, quotes every cell, preserves original numeric strings and guards formula-like text/leading-zero text IDs. Spreadsheet importers choose their own types; canonical programmatic consumers can use the typed API. The chart consumes the stored metric instead of implementing another authoritative formula.

See `docs/FRONTEND.md`, `docs/API.md`, `docs/SECURITY.md`, `DECISIONS.md` sections 27–31 and `NOTES.md` for the module map, trust boundaries and actual design corrections.

## Automated verification

All root runtime dependency writes finished before the final browser acceptance. Browser tests have **one worker, zero retries**, fresh synthetic state and `DEBUG=False` Waitress on port 8766. They never open the operator's `key.txt`, `.service/local-config.json` or real data. Successful preview/SQL requests use the real compiler/WASM path; refreshes use a separate real worker with an explicitly synthetic source client. Interception is confined to controlled loading/error/denial/retry/stale-response scenarios.

| Run | Observed result |
| --- | --- |
| Root Python 3.14.2 complete suite before the two later Part 2 overflow regressions | **653 passed, 1 skipped**, 209.43 seconds |
| Root Part 2 follow-up, including those two added regressions | **85 passed**, 63.11 seconds; its 13 publication cases also pass Python 3.12.14 |
| Final root JavaScript components | **17 passed**, no skips/failures |
| Root Chromium complete browser suite | **18 passed**, no skips/flaky cases/retries, 182.13 seconds; ten axe scans with zero automated violations, ten desktop/mobile screenshots |
| Follow-up after two ARIA semantics and SVG axis color corrections | **3 passed**, 26.31 seconds; ten axe scans with zero automated violations; actual rendered axis contrast **5.03:1** against white |
| Fresh checkout at `fcf529b` with new Python 3.12 environment | **655 passed, 1 skipped**, 213.08 seconds; **17 JS passed**; **18 Chromium passed**, no skips/flaky cases, 173.84 seconds; ten axe scans/zero automated violations and ten screenshots; lint/format/pip/migration checks pass |
| Final 320px layout follow-up at `a9c6db0` | **3 passed**, 30.79 seconds; ten axe scans/zero automated violations; all four metric values have exactly one rendered text fragment, matching line-box heights and fitting card content bounds at 320px |
| Installed 0.4.0 wheel, outside repository source | **Passed** on Python 3.14.2 with isolated imports/noneditable install and `DEBUG=False` Waitress; all three personas, real compiler/WASM, exact DATE/DECIMAL results, pagination/CSV, 12 assets/ETag/CSP/allowlist checks, 390/320px layout; six axe scans/zero automated violations, eight screenshots |

The sole Python skip is Windows symbolic-link privilege availability; it is not a passing symlink-boundary claim. The configured Linux CI matrix has not been executed on this host. Browser acceptance here uses Chromium on Windows, not an assertion of Safari/Firefox support. Axe incomplete checks remain recorded; a zero-violation scan does not certify full WCAG conformance. Keyboard flows and visual desktop/mobile review provide separate evidence.

The main root browser record is `work/browser-results/evidence.json`, backed by its full JSON/HTML report and hashed screenshots. The ARIA/contrast follow-up is separate at `work/browser-final-a11y-evidence.json`; it does not overwrite the full 18-case proof. Fresh-checkout evidence is `work/part4-clean-checkout-evidence.json`, with baseline artifacts preserved under `work/part4-clean-checkout-full18-fcf529b/` and final targeted artifacts separately. The only changes after the complete fresh suite were three narrow-layout CSS lines and their accessibility regression; backend/dependency files were verified unchanged. The final test corrections and the two preserved intermediate harness failures are recorded explicitly.

Wheel evidence and exact commands are in `work/part4-wheel/acceptance.json` and `COMMANDS.md`. Its 68 packaged files byte-match archived production commit `4820621`; later commits changed tests/documentation only. Wheel SHA-256 is `a4c96851f85a559947afdc7cc07e39f7ffff68dbae407406e3e14293a251e22a`. Its independently configured trusted Node modules were the pinned runtime, not bundled npm dependencies inside the Python wheel. The standalone driver's two initial setup/assertion failures and corrected successful run are retained; application bytes were unchanged between attempts. The temporary wheel server was stopped and its port released.

An earlier browser attempt received one generic 422 for a valid small query while a root npm dependency install was actively replacing runtime files. The exact underlying child error cannot be reconstructed from that public response, so the overlap is not claimed as proven causation. With dependency writes frozen, 35 direct repetitions passed, Windows peak process use remained below its unchanged 512 MiB limit, and the complete root browser run passed. Traces and repeat-probe receipts remain under ignored `work/`. No runtime limit was raised and no silent query-error retry was introduced to hide the failure.

## Real-data UI acceptance

The final local UI was exercised with the generated real Viewer, Analyst and Admin accounts against **1,160,019** persisted EIA observations. Real credentials were read privately for the test; no HAR, trace, video or screenshot of filled credentials was recorded.

- Analyst displayed ten authorized dataset choices, actual rows and a successful 100-row SQL result, with no page errors or failed requests.
- Viewer at 390px displayed two dataset choices, no Admin pane markup and no body overflow. Final desktop/mobile/login/explorer/SQL/refresh screenshots are retained in `work/part4-real-visual/`; the README includes the real desktop overview.
- Admin UI queued recent refresh **`13ce7c59-bc48-43b9-a779-a58c2ee812a6`**. A separately invoked real EIA worker succeeded in **246.594 seconds**, publishing source **`9b3e376033ba44298d86cbf754b85335`** and model **`a9163784f25249189aca6c2877c17889`**. The old workspace model remained visible until explicit adoption; the server session scope stayed unchanged. UI outcome screenshots/receipts are in `work/part4-live-ui/` and sanitized worker output in `work/part4-live-worker.*`.
- The adopted overview showed October 30, 2026, capacity **100,056.7 MW**, outage **17,542.627 MW**, and stored offline percentage **17.532685967%** (card presentation 17.53%). Current integrity verification and independent raw/model comparison passed; three raw and ten model logical hashes match the retained baseline.
- A separate real-data check applied September 3 only and displayed exactly **55** facility rows, with all dates equal to the applied date and both unnecessary paging controls disabled. A valid empty SQL query showed the empty state and hid prior results. At **320px**, the overview document remained exactly 320px wide; no page errors occurred. Screenshots/receipt are in `work/part4-final-ui-states/`.

## Reproduce

The final release audit found none of the five actual supplied/generated credential values in any audited Git object (including unreachable objects), tracked files, persisted data JSON, work-root logs/reports, service/catalog SQLite bytes or the 65 Parquet files and 270 decompressed VARCHAR columns. The credential files are ignored and untracked. Exact scope counts and the clean-tree/current-service checks are in `work/part4-secret-audit.json` and `work/part4-final-completion-audit.json`; these are execution-scoped results, not a promise about later edits.

Follow the README setup, then open `http://127.0.0.1:8000/login/`. Generated `viewer`, `analyst`, `admin` passwords live only in the private ignored local configuration. Existing local data is ready; a fresh source checkout needs EIA ingestion for real data. Browser tests construct synthetic data without a key.

```powershell
npm run test:unit
npx playwright install chromium
npm run test:browser
node tests/browser/summarize.cjs
.\.venv\Scripts\python.exe -m pytest
```

Run dependency installation before starting acceptance, and keep test port 8766 free. An Admin refresh stays queued until the separately configured worker is running with `EIA_API_KEY`. The web process does not read that credential. See the README for the operator commands.

Hosted HTTPS/Linux runtime acceptance, backup/restore, larger-volume load tests, cross-session saved investigations and assistive-technology user testing remain explicit follow-up work. They are not represented as completed Azure deployment or accessibility certification.
