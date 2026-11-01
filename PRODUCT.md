# Outage Explorer — product notes

Outage Explorer turns published EIA observations into a focused workspace for following the U.S. nuclear fleet. Viewer follows national trends; Analyst explores all analytical datasets and writes SQL; Admin also refreshes the publication. The interface uses the existing authenticated API throughout.

**Stories added.** Keep chart, table and SQL on one immutable publication while a refresh runs. Recover from empty results, invalid queries and busy capacity without mistaking stale rows for a new answer. Export the observations currently shown, with enough version context to reproduce the question.

**Key UX decisions.** A national overview opens with the stored US-08 metric, its MW numerator/denominator and observation date. Calendar gaps and undefined ratios break the line; they never become zeros. Exact values remain available in a keyboard-accessible table. Dates are observation dates, not timestamps converted to the browser's zone.

Dataset choices and schema suggestions come from the permitted catalog. Filters show the applied period/facility; backend cursors preserve that question and version. SQL deliberately matches the backend's single-dataset SELECT language. Schema insertion, Ctrl+Enter and the last ten queries make it usable without introducing a second query engine in the browser. History is private to the current server session and is cleared when access changes or the user signs out; results and credentials are not stored in browser history storage.

A completed refresh announces a newer publication. Users choose when to apply it, so an active investigation does not silently change. Queued/running progress is indeterminate; outcome states are explicit. No percentage is invented while the worker is busy.

CSV exports include the shown page, not an undisclosed background download. Numeric strings keep their precision. Formula-like text and leading-zero text IDs are prefixed with an apostrophe for spreadsheet safety; importers needing canonical types can use the typed API. The layout adapts to small screens, with native controls, visible keyboard focus, announced errors and scrollable tables.

**Left out and next.** This version does not add a dashboard builder, cross-session saved queries, multi-table SQL or cloud deployment. Next, validate the hosted runtime and operator recovery, test with actual analysts and assistive-technology users, then prioritize saved investigations and richer filtering from that feedback. Automated accessibility scans support the basic checks; they do not replace user testing.
