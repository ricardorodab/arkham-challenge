"""Deterministic source simulator; these rows are synthetic, not EIA findings."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta

from outage_explorer.domain import ROUTES, UNITS
from outage_explorer.eia import EIAClientError, Page, RouteMetadata, sanitize

SECRET = "test-secret-canary-not-a-real-api-key"


def source_rows():
    result = {spec.route: [] for spec in ROUTES}
    for delta in range(3):
        period = str(date(2026, 9, 1) + timedelta(days=delta))
        for spec in ROUTES:
            entities = [()] if spec.name == "national" else [(f,) for f in ("001", "002")]
            if spec.name == "generator":
                entities = [(f, g) for f in ("001", "002") for g in ("01", "02")]
            for entity in entities:
                row = {
                    "period": period,
                    "capacity": "100.25",
                    "outage": "10.025",
                    "percentOutage": "10.00",
                    **{f"{k}-units": v for k, v in UNITS.items()},
                }
                row.update(dict(zip(spec.key_fields[1:], entity, strict=True)))
                if entity:
                    row["facilityName"] = "Synthetic facility"
                result[spec.route].append(row)
    return result


class FakeClient:
    def __init__(self, rows=None):
        self.rows = deepcopy(rows if rows is not None else source_rows())
        self.requests = self.retries = self.pages = 0
        self.declared_totals = {}
        self.calls = []
        self.fail_route = None
        self.fail_after_pages = 0
        self.raised_error = EIAClientError(
            "network_error", "Simulated network failure.", retryable=True
        )
        self.confirmation_mutation = None
        self._calls_by_route = {}

    @property
    def statistics(self):
        return {
            "requests": self.requests,
            "retries": self.retries,
            "pages": self.pages,
            "declared_totals": dict(self.declared_totals),
        }

    def sanitize(self, value):
        return sanitize(value, (SECRET,))

    def fetch_metadata(self, route):
        self.requests += 1
        spec = next(spec for spec in ROUTES if spec.route == route)
        rows = self.rows[route]
        start = min(date.fromisoformat(row["period"]) for row in rows)
        end = max(date.fromisoformat(row["period"]) for row in rows)
        fields = {k: {"units": v} for k, v in UNITS.items()}
        raw = {
            "response": {"startPeriod": str(start), "endPeriod": str(end)},
            "request": {"params": {"api_key": SECRET}},
        }
        return RouteMetadata(route, start, end, fields, raw, ("daily",), spec.key_fields[1:])

    def iter_pages(self, route, start, end, page_size=5000):
        self.calls.append((route, start, end))
        self._calls_by_route[route] = self._calls_by_route.get(route, 0) + 1
        rows = deepcopy(
            [row for row in self.rows[route] if str(start) <= row["period"] <= str(end)]
        )
        if self.confirmation_mutation and self._calls_by_route[route] > 1:
            rows = self.confirmation_mutation(route, rows)
        for page_number, offset in enumerate(range(0, len(rows), page_size)):
            if route == self.fail_route and page_number >= self.fail_after_pages:
                raise self.raised_error
            self.requests += 1
            self.pages += 1
            self.declared_totals[route] = len(rows)
            yield Page(
                rows[offset : offset + page_size],
                len(rows),
                offset,
                {"request": {"params": {"api_key": SECRET}}},
            )
        self.requests += 1
        self.declared_totals[route] = len(rows)
