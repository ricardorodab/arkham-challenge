# EIA HTTP fixtures

The `eia_metadata.json`, `eia_national_page.json`, `eia_empty_page.json`,
`eia_invalid_key.json`, and `eia_rate_limit.json` files are **representative,
sanitized EIA-shaped test fixtures**, not claims of complete recorded HTTP
sessions. The national September 1, 2026 observation was seen during the planning
investigation. The deliberately inconsistent `total` tests pagination behavior;
it must not be interpreted as a national dataset finding.

Failure payloads reproduce the documented API error shapes without credentials.
Tests inject HTTP status and Retry-After headers separately. Actual integration
recordings, when added, must identify their collection period and be sanitized
before persistence. Fixtures are evidence for client behavior, not for complete
source coverage or analytical findings.
