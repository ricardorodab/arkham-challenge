# Sanitized responses recorded from EIA

The six metadata/data JSON files are **actual EIA API responses**, collected on October 31,
2026 in `America/Los_Angeles` (November 1, 2026 UTC), then sanitized by the
connector before persistence. Their exact source recording timestamps and SHA-256
hashes are in `provenance.json`. The timestamps describe local recording, not an
EIA publication or revision time.

Collection run: `662caeb500d1433ab55a5df82d403ca8`.

- Metadata: each of the three nuclear-outage routes.
- Data: the **first page only** from each route, for `2026-09-01` through
  `2026-09-03`, requested with `length=50` and stable sorting by the full key.
- National first page: 3 rows.
- Facility and generator first pages: 50 rows each; neither is a complete extract.
- These request echoes omit `api_key`; no usable credentials are included.
  Metadata echoes have an empty `params` list. The connector also redacts an
  `api_key` value if EIA returns one in another response.

The collection run subsequently failed on a Windows filesystem synchronization
operation. The responses are genuine; their presence does **not** prove a
successful snapshot publication, complete source coverage, or analytical findings.

Replay tests use these metadata and data pages to exercise client and domain
compatibility offline. Tests that append an empty terminal page label that page
as **synthetic**: they demonstrate the client's protocol handling, not that EIA
actually ended the facility or generator result after fifty rows.

`invalid_credentials.json` is an additional **actual HTTP 403 response**, captured
using an intentionally invalid credential in a separate failure probe. Its wrapper
preserves the HTTP status and sanitized response body. It is explicitly marked
`outside_capture_run` in the provenance and is not part of the six successful HTTP
responses above. Replay verifies that rejected credentials cause one request and
no retries, without using any real API key or live network.
