"""Provide synthetic input and private-volume inspection for Docker acceptance only.

This file is mounted read-only at /acceptance after the production image is built. It is
never an application endpoint or a deployment default. Every stateful command requires the
container's explicit /appdata paths and reads only that acceptance project's named volumes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import stat
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

SYNTHETIC_KEY = "docker-acceptance-synthetic-source-only"


def source_rows(revision=0):
    """Return coherent three-day observations, with exact decimals and textual IDs."""
    from outage_explorer.data.domain import ROUTES, UNITS

    rows = {spec.route: [] for spec in ROUTES}
    for period, percent in zip(
        ("2026-09-01", "2026-09-02", "2026-09-03"), (10, 0, 20 + revision * 10), strict=True
    ):
        for spec in ROUTES:
            entities = [()]
            if spec.name == "facility":
                entities = [(facility,) for facility in ("001", "002")]
            elif spec.name == "generator":
                entities = [
                    (facility, unit) for facility in ("001", "002") for unit in ("01", "02")
                ]
            capacity = {"national": "401", "facility": "200.5", "generator": "100.25"}[spec.name]
            for entity in entities:
                row = {
                    "period": period,
                    "capacity": capacity,
                    "outage": str(Decimal(capacity) * Decimal(percent) / 100),
                    "percentOutage": str(percent),
                    **{f"{metric}-units": unit for metric, unit in UNITS.items()},
                    **dict(zip(spec.key_fields[1:], entity, strict=True)),
                }
                if entity:
                    row["facilityName"] = "Synthetic Docker facility"
                rows[spec.route].append(row)
    return rows


class FakeClient:
    """Replace upstream I/O while leaving validation, publication and worker code real."""

    def __init__(self, key=SYNTHETIC_KEY, *, revision=0, failure=False):
        if key != SYNTHETIC_KEY:
            raise RuntimeError("Only an explicit synthetic upstream key is allowed.")
        self.rows = source_rows(revision)
        self.failure = failure
        self.requests = self.pages = 0
        self.declared_totals = {}

    @property
    def statistics(self):
        return {
            "requests": self.requests,
            "retries": 0,
            "pages": self.pages,
            "declared_totals": dict(self.declared_totals),
        }

    def sanitize(self, value):
        from outage_explorer.data.eia import sanitize

        return sanitize(value, (SYNTHETIC_KEY,))

    def fetch_metadata(self, route):
        from outage_explorer.data.domain import ROUTES, UNITS
        from outage_explorer.data.eia import RouteMetadata

        self.requests += 1
        spec = next(spec for spec in ROUTES if spec.route == route)
        return RouteMetadata(
            route,
            date(2026, 9, 1),
            date(2026, 9, 3),
            {metric: {"units": unit} for metric, unit in UNITS.items()},
            {"response": {"startPeriod": "2026-09-01", "endPeriod": "2026-09-03"}},
            ("daily",),
            spec.key_fields[1:],
        )

    def iter_pages(self, route, start, end, page_size=5000):
        from outage_explorer.data.eia import EIAClientError, Page

        if self.failure and route == "generator-nuclear-outages":
            raise EIAClientError("network_error", "Synthetic upstream failure.", retryable=True)
        rows = [row for row in self.rows[route] if str(start) <= row["period"] <= str(end)]
        self.declared_totals[route] = len(rows)
        for offset in range(0, len(rows), page_size):
            self.requests += 1
            self.pages += 1
            yield Page(rows[offset : offset + page_size], len(rows), offset, {"response": {}})
        self.requests += 1


def configuration():
    """Read only the isolated container volume, never a cwd or host fallback."""
    for name, expected in (
        ("OUTAGE_STATE_DIR", "/appdata/state"),
        ("OUTAGE_DATA_DIR", "/appdata/data"),
    ):
        if os.environ.get(name) != expected:
            raise RuntimeError("Docker acceptance requires explicit isolated volume paths.")
    path = Path("/appdata/state/local-config.json")
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError("Generated configuration is not a private regular file.")
    return json.loads(path.read_text(encoding="utf-8"))


def setup():
    """Configure the installed Django project using this new volume's generated key."""
    config = configuration()
    os.environ["OUTAGE_DJANGO_SECRET_KEY"] = config["OUTAGE_DJANGO_SECRET_KEY"]
    os.environ["DJANGO_SETTINGS_MODULE"] = "outage_explorer.web.settings"
    import django

    django.setup()
    return config


def image_metadata():
    """Report public dependency/hash metadata from an image without state volumes."""
    from outage_explorer.query import query_sandbox

    prohibited = (
        "/opt/outage/.env",
        "/opt/outage/key.txt",
        "/opt/outage/.service",
        "/opt/outage/data",
        "/opt/outage/work",
        "/opt/outage/docs/AI",
        "/opt/outage/tests",
        "/appdata/state/local-config.json",
        "/appdata/state/web.sqlite3",
        "/appdata/data/catalog.sqlite3",
    )
    modules = Path("/opt/outage/node_modules/@duckdb/duckdb-wasm")
    assets = {
        "cjs": modules / "dist/duckdb-node-blocking.cjs",
        "wasm": modules / "dist/duckdb-eh.wasm",
        "worker": query_sandbox.ASSETS / "worker.cjs",
        "extension": query_sandbox.ASSETS / "parquet-v1.5.4-wasm_eh.wasm",
    }
    return {
        "uid": os.getuid(),
        "python": platform.python_version(),
        "node": subprocess.check_output(["node", "--version"], text=True).strip(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("asgiref", "Django", "duckdb", "sqlglot", "sqlparse", "tzdata", "waitress")
        },
        "npm_version": json.loads((modules / "package.json").read_text())["version"],
        "runtime": query_sandbox.runtime_status(),
        "asset_sha256": {
            name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in assets.items()
        },
        "prohibited_files_present": [path for path in prohibited if Path(path).exists()],
    }


def state():
    """Fingerprint credentials/accounts and expose matching publication/job metadata."""
    from django.conf import settings
    from django.contrib.auth import get_user_model

    from outage_explorer.data.storage import Catalog
    from outage_explorer.service.models import RefreshJob

    with Catalog(settings.OUTAGE_DATA_DIR) as catalog:
        raw = catalog.current()
        model = catalog.current_model()
    users = list(get_user_model().objects.order_by("username").values_list("username", "password"))
    return {
        "uid": os.getuid(),
        "configuration_sha256": hashlib.sha256(
            Path("/appdata/state/local-config.json").read_bytes()
        ).hexdigest(),
        "accounts_sha256": hashlib.sha256(json.dumps(users).encode()).hexdigest(),
        "snapshot_id": raw.id if raw else None,
        "model_id": model.id if model else None,
        "model_source_id": model.manifest["source_snapshot_id"] if model else None,
        "jobs": list(RefreshJob.objects.order_by("created_at").values("id", "status", "result")),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("image", "credentials", "state", "seed", "worker"))
    parser.add_argument("--failure", action="store_true")
    args = parser.parse_args()
    if args.action == "image":
        result = image_metadata()
    else:
        config = setup()
        if args.action == "credentials":
            # Captured privately by the host harness; never included in its output/report.
            result = {
                role: config[f"OUTAGE_{role.upper()}_PASSWORD"]
                for role in ("viewer", "analyst", "admin")
            }
        elif args.action == "seed":
            from django.conf import settings

            from outage_explorer.data.pipeline import IngestConfig, ingest

            ingest(IngestConfig(settings.OUTAGE_DATA_DIR, "full"), FakeClient())
            result = state()
        elif args.action == "worker":
            import time

            from outage_explorer.service.job_progress import JobProgressReporter
            from outage_explorer.web import local

            class ObservableReporter(JobProgressReporter):
                """Expose real stage writes long enough for host HTTP observations."""

                last_stage = None

                def __call__(self, event):
                    super().__call__(event)
                    if event.stage != self.last_stage:
                        self.last_stage = event.stage
                        time.sleep(0.35)

            os.environ["EIA_API_KEY"] = SYNTHETIC_KEY
            sys.argv = ["docker-acceptance-worker", "worker", "--once"]
            with (
                patch(
                    "outage_explorer.service.refresh.EIAClient",
                    lambda key: FakeClient(key, revision=1, failure=args.failure),
                ),
                patch("outage_explorer.service.refresh.JobProgressReporter", ObservableReporter),
            ):
                local.main()
            return
        else:
            result = state()
    print(json.dumps(result, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
