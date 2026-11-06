"""Build and reset the browser suite's synthetic data and operational state.

configure confines paths to a validated run-ID beneath work/browser-state and
work/browser-data before Django initializes. Helpers intentionally mutate only that fixture
environment; never call reset against an operator configuration.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((Path(__file__).parent / "fixture.json").read_text(encoding="utf-8"))


def configure() -> None:
    """Validate the run ID and select synthetic settings before Django setup.

    Remove the inherited EIA key and supply fixture-only signing/seed values. The separate
    Node environment helper also strips inherited Python/seed settings. This configures paths
    but does not launch a service, apply migrations or publish.
    """
    run_id = os.environ.get("OUTAGE_BROWSER_RUN_ID", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", run_id):
        raise RuntimeError("A browser-only isolated run identifier is required.")
    os.environ.update(
        DJANGO_SETTINGS_MODULE="outage_explorer.web.settings",
        OUTAGE_PROJECT_DIR=str(ROOT),
        OUTAGE_STATE_DIR=str(ROOT / "work/browser-state" / run_id),
        OUTAGE_DATA_DIR=str(ROOT / "work/browser-data" / run_id),
        OUTAGE_DJANGO_SECRET_KEY=FIXTURE["secret_key"],
        OUTAGE_DJANGO_DEBUG="0",
        OUTAGE_LOCAL_HTTP="1",
    )
    os.environ.pop("EIA_API_KEY", None)
    for role, password in FIXTURE["passwords"].items():
        os.environ[f"OUTAGE_{role.upper()}_PASSWORD"] = password


def source_rows(*, revision: int = 0):
    """Return coherent three-grain rows for metric, gap and hostile-text UI checks.

    Dates come from the fixture's deliberately gapped period list. Daily percentages are 10, 0
    and 20 plus ten times revision, so revision one changes the final value to 30. Zero-padded
    IDs and formula/HTML-looking facility names exercise exact text rendering/export rather
    than representing actual EIA observations.
    """
    from outage_explorer.domain import ROUTES, UNITS

    rows = {spec.route: [] for spec in ROUTES}
    percentages = [10, 0, 20 + 10 * revision]
    for period, percentage in zip(FIXTURE["periods"], percentages, strict=True):
        for spec in ROUTES:
            entities = [()] if spec.name == "national" else [(f,) for f in ("001", "002")]
            if spec.name == "generator":
                entities = [(f, f"{g:02d}") for f in ("001", "002") for g in range(1, 21)]
            for entity in entities:
                capacity = {"national": 2000, "facility": 1000, "generator": 50}[spec.name]
                row = {
                    "period": period,
                    "capacity": str(capacity),
                    "outage": str(Decimal(capacity) * Decimal(percentage) / 100),
                    "percentOutage": str(percentage),
                    **{f"{metric}-units": unit for metric, unit in UNITS.items()},
                }
                row.update(zip(spec.key_fields[1:], entity, strict=True))
                if entity:
                    row["facilityName"] = (
                        "=2+3"
                        if entity[0] == "001"
                        else '<img src=x onerror="window.__browserXss=1">'
                    )
                rows[spec.route].append(row)
    return rows


def publish(*, revision: int = 0, fail: bool = False, mode: str = "full"):
    """Build/publish fixture data through the real pipeline using a fake upstream.

    An optional generator-route failure tests preservation of prior publication. Return the
    matching source/model IDs; a separately invoked synthetic worker uses this executor to
    exercise durable refresh without an upstream credential.
    """
    from django.conf import settings

    from outage_explorer.pipeline import IngestConfig, ingest
    from outage_explorer.storage import Catalog
    from tests.helpers import FakeClient

    client = FakeClient(source_rows(revision=revision))
    if fail:
        client.fail_route = "generator-nuclear-outages"
    manifest = ingest(IngestConfig(settings.OUTAGE_DATA_DIR, mode), client)
    with Catalog(settings.OUTAGE_DATA_DIR) as catalog:
        return {"snapshot_id": manifest["snapshot_id"], "model_id": catalog.current_model().id}


def reset() -> None:
    """Restore fixture users, sessions/cache/login buckets and baseline publication.

    The caller must first configure Django with run-ID-confined synthetic paths. Clear
    private operational rows and interrupt active fixture jobs, then republish the baseline
    only when the selected model differs from its stored fixture marker. This is destructive
    test reset behavior, not an operator recovery command.
    """
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.contrib.sessions.models import Session
    from django.core.management import call_command
    from django.utils import timezone

    from outage_explorer.service.models import Persona, QueryCacheEntry, RateLimitBucket, RefreshJob
    from outage_explorer.storage import Catalog

    for role in FIXTURE["passwords"]:
        user = get_user_model().objects.filter(username=role).first()
        if user is None:
            continue
        get_user_model().objects.filter(pk=user.pk).update(
            is_active=True, is_staff=False, is_superuser=False
        )
        Persona.objects.update_or_create(user_id=user.pk, defaults={"role": role})
    call_command("seed_users", verbosity=0)
    Session.objects.all().delete()
    QueryCacheEntry.objects.all().delete()
    RateLimitBucket.objects.all().delete()
    RefreshJob.objects.filter(status__in=["queued", "running"]).update(
        status="interrupted", finished_at=timezone.now(), error_code="synthetic_fixture_reset"
    )
    baseline_file = settings.OUTAGE_STATE_DIR / "synthetic-baseline.json"
    baseline = (
        json.loads(baseline_file.read_text(encoding="utf-8")) if baseline_file.exists() else {}
    )
    with Catalog(settings.OUTAGE_DATA_DIR) as catalog:
        current = catalog.current_model()
    if current is None or current.id != baseline.get("model_id"):
        baseline_file.write_text(json.dumps(publish()), encoding="utf-8")
