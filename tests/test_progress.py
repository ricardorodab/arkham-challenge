"""Exercise progress against synthetic pages and real local publication boundaries."""

from collections import Counter
from contextlib import nullcontext
from dataclasses import FrozenInstanceError, fields, replace
from datetime import date
from threading import Event, Lock
from types import SimpleNamespace

import duckdb
import pytest

from outage_explorer.analytics import model_store, modeling
from outage_explorer.analytics.model_contracts import DATASETS
from outage_explorer.data import pipeline
from outage_explorer.data.domain import ROUTES as SOURCE_ROUTES
from outage_explorer.data.domain import DataValidationError, canonical_json
from outage_explorer.data.pipeline import IngestConfig, ingest
from outage_explorer.data.storage import Catalog, StorageError
from outage_explorer.progress import ROUTES, STAGES, ProgressEvent, emit_progress
from tests.helpers import FakeClient, source_rows


def test_event_contract_is_frozen_and_exports_only_known_stages_and_grains():
    event = ProgressEvent("extracting", route="national", rows_received=2, pages_fetched=1)
    with pytest.raises(FrozenInstanceError):
        event.rows_received = 3
    assert [item.name for item in fields(event)] == [
        "stage",
        "route",
        "rows_received",
        "pages_fetched",
        "completed",
        "total",
    ]
    assert ROUTES == {spec.name for spec in SOURCE_ROUTES}
    assert "exporting_model" in STAGES and "completed" in STAGES
    assert event.completed is None and event.total is None


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(7)])
def test_observers_do_not_swallow_process_control_exceptions(error):
    def interrupted(event):
        raise error

    with pytest.raises(type(error)) as caught:
        emit_progress(interrupted, ProgressEvent("preparing"))
    assert caught.value is error
    emit_progress(None, ProgressEvent("preparing"))


def test_parallel_page_deltas_and_export_checkpoints_use_completed_work(tmp_path):
    class MisleadingTotals(FakeClient):
        def iter_pages(self, *args, **kwargs):
            for page in super().iter_pages(*args, **kwargs):
                yield replace(page, declared_total=10**9)

    events, exports = [], []
    lock = Lock()

    def observed(event):
        with lock:
            events.append(event)
            if event.stage == "exporting_model":
                exports.append(
                    (event, len(list((tmp_path / ".model-staging").glob("*/*.parquet"))))
                )

    client = MisleadingTotals()
    ingest(IngestConfig(tmp_path, "full", page_size=2, workers=3), client, progress=observed)
    page_events = [event for event in events if event.pages_fetched]
    assert len(page_events) == client.pages == 11
    assert sum(event.rows_received for event in page_events) == 21
    rows, pages = Counter(), Counter()
    for event in page_events:
        rows[event.route] += event.rows_received
        pages[event.route] += event.pages_fetched
        assert event.stage == "extracting" and event.pages_fetched == 1
        assert event.completed is None and event.total is None
    assert rows == {"national": 3, "facility": 6, "generator": 12}
    assert pages == {"national": 2, "facility": 3, "generator": 6}
    phases = [event.stage for event in events]
    assert phases[0] == "preparing" and phases[-1] == "completed"
    last_page = max(index for index, event in enumerate(events) if event.pages_fetched)
    assert phases.index("validating_source") > last_page
    assert "extracting" not in phases[phases.index("validating_source") :]
    assert all(event.stage in STAGES and event.route in ROUTES | {None} for event in events)
    assert [event.completed for event, _ in exports] == list(range(len(DATASETS) + 1))
    assert all(
        event.total == len(DATASETS) and count == event.completed for event, count in exports
    )
    assert all(
        event.completed is None and event.total is None
        for event in events
        if event.stage != "exporting_model"
    )


def test_confirmation_rereads_count_as_received_work_without_inflating_publication(tmp_path):
    ingest(IngestConfig(tmp_path, "full"), FakeClient())
    rows = source_rows()
    rows[SOURCE_ROUTES[2].route].pop(0)
    events = []
    client = FakeClient(rows)
    manifest = ingest(IngestConfig(tmp_path, "recent", page_size=2), client, progress=events.append)
    pages = [event for event in events if event.pages_fetched]
    assert sum(event.pages_fetched for event in pages) == client.pages == 17
    assert sum(event.rows_received for event in pages) == 31
    assert sum(event.rows_received for event in pages if event.route == "generator") == 22
    assert manifest["routes"]["generator"]["extracted_rows"] == 11
    assert manifest["routes"]["generator"]["rows"] == 11
    assert manifest["routes"]["generator"]["deletions_confirmed"]


def test_invalid_received_page_is_not_reported_before_validation_and_insertion(tmp_path):
    rows = source_rows()
    rows[SOURCE_ROUTES[0].route][1]["capacity"] = "invalid"
    client = FakeClient(rows)
    events = []
    with duckdb.connect(config={"threads": 1}) as connection:
        with pytest.raises(DataValidationError):
            pipeline._extract(
                connection,
                SOURCE_ROUTES[0],
                client,
                IngestConfig(tmp_path, "full", page_size=1),
                date(2026, 9, 1),
                date(2026, 9, 3),
                "incoming",
                tmp_path,
                None,
                Event(),
                progress=events.append,
            )
        assert connection.execute("SELECT count(*) FROM incoming").fetchone()[0] == 1
    assert client.pages == 2
    assert events == [
        ProgressEvent("extracting", route="national", rows_received=1, pages_fetched=1)
    ]


def test_phase_events_precede_work_and_completed_follows_publication(tmp_path, monkeypatch):
    events, checkpoints = [], []
    published = False
    completed_after_publish = []

    def observed(event):
        events.append(event)
        if event.stage == "completed":
            completed_after_publish.append(published)

    class CheckedClient(FakeClient):
        def fetch_metadata(self, route):
            assert events[-1].stage == "metadata"
            return super().fetch_metadata(route)

        def iter_pages(self, *args, **kwargs):
            assert events[-1].stage == "extracting"
            yield from super().iter_pages(*args, **kwargs)

    def checked(original, stage):
        def wrapped(*args, **kwargs):
            assert events[-1].stage == stage
            checkpoints.append(stage)
            return original(*args, **kwargs)

        return wrapped

    for name, stage in (
        ("_source_contract", "model_sources"),
        ("_facts_and_dimensions", "model_facts"),
        ("_fleet", "model_fleet"),
        ("_reconciliation", "model_reconciliation"),
        ("_events", "model_quality"),
        ("validate_model_tables", "validating_model"),
    ):
        monkeypatch.setattr(modeling, name, checked(getattr(modeling, name), stage))
    original_verify = model_store.verify_model

    def verified(*args, **kwargs):
        assert events[-1].stage in {"verifying_model", "publishing"}
        checkpoints.append(events[-1].stage)
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(model_store, "verify_model", verified)
    original_publish = Catalog.publish

    def publish(*args, **kwargs):
        nonlocal published
        assert events[-1].stage == "publishing"
        result = original_publish(*args, **kwargs)
        published = True
        return result

    monkeypatch.setattr(Catalog, "publish", publish)
    ingest(IngestConfig(tmp_path, "full"), CheckedClient(), progress=observed)
    assert checkpoints[:7] == [
        "model_sources",
        "model_facts",
        "model_fleet",
        "model_reconciliation",
        "model_quality",
        "validating_model",
        "verifying_model",
    ]
    assert completed_after_publish == [True]


def test_observer_failures_leave_both_publications_and_logical_content_intact(tmp_path):
    first = ingest(IngestConfig(tmp_path, "full"), FakeClient())
    with Catalog(tmp_path) as catalog:
        first_model = catalog.current_model()
    observed = []

    def broken(event):
        observed.append(event.stage)
        raise OSError("Synthetic observer failure")

    second = ingest(IngestConfig(tmp_path, "full"), FakeClient(), progress=broken)
    with Catalog(tmp_path) as catalog:
        assert catalog.current().id == second["snapshot_id"]
        second_model = catalog.current_model()
        assert second_model.manifest["source_snapshot_id"] == second["snapshot_id"]
        assert catalog.history()[0]["status"] == "succeeded"
        assert catalog.model_history()[0]["status"] == "succeeded"
    assert observed[-1] == "completed"
    assert set(first) == set(second)
    assert "rows_received" not in canonical_json(second)
    assert "pages_fetched" not in canonical_json(second_model.manifest)
    for route in ROUTES:
        assert first["routes"][route]["logical_sha256"] == second["routes"][route]["logical_sha256"]
    for dataset in DATASETS:
        assert (
            first_model.manifest["datasets"][dataset]["logical_sha256"]
            == second_model.manifest["datasets"][dataset]["logical_sha256"]
        )


@pytest.mark.parametrize("receipt_available", [False, True])
def test_pipeline_completion_waits_for_job_receipt_and_worker_outcome(
    monkeypatch, receipt_available
):
    from outage_explorer.service import refresh

    source_id, model_id = "a" * 32, "b" * 32
    events = []
    model = SimpleNamespace(id=model_id, manifest={"source_snapshot_id": source_id})
    catalog = SimpleNamespace(
        current_model=lambda: model if receipt_available else None,
        model_history=lambda **kwargs: [],
    )

    def published(config, client, *, progress):
        progress(ProgressEvent("publishing"))
        progress(ProgressEvent("completed"))
        return {"snapshot_id": source_id}

    monkeypatch.setenv("EIA_API_KEY", "synthetic-progress-test-key")
    monkeypatch.setattr(refresh, "EIAClient", lambda key: FakeClient())
    monkeypatch.setattr(refresh, "ingest", published)
    monkeypatch.setattr(refresh, "Catalog", lambda root: nullcontext(catalog))
    if receipt_available:
        assert refresh.perform_ingest("full", progress=events.append) == {
            "snapshot_id": source_id,
            "model_id": model_id,
            "mode": "full",
        }
    else:
        with pytest.raises(StorageError, match="could not be verified"):
            refresh.perform_ingest("full", progress=events.append)
    assert events == [ProgressEvent("publishing")]
