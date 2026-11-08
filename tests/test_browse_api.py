"""Exercise real isolated browsing, signed continuations and authorization over saved rows."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal

import duckdb
import pytest
from django.core import signing

from outage_explorer.analytics.model_contracts import DATASETS
from outage_explorer.analytics.modeling import ModelBundle
from outage_explorer.service import api_services
from outage_explorer.service.models import Persona


@pytest.fixture(scope="module")
def browse_bundle(tmp_path_factory):
    """Create 5,005 typed rows with exact decimals, nulls, duplicate sorts and hostile text."""
    directory = tmp_path_factory.mktemp("browse-api")
    dataset = "generator_daily"
    spec = DATASETS[dataset]
    path = directory / f"{dataset}.parquet"
    with duckdb.connect(config={"threads": 1, "memory_limit": "128MB"}) as db:
        db.execute("CREATE TABLE source (" + ",".join(f'"{n}" {t}' for n, t in spec.columns) + ")")
        rows = []
        for number in range(5005):
            rows.append(
                (
                    date(2026, 9, 1),
                    "001",
                    f"{number:05d}",
                    "O'Brien %_ Ω Needle"
                    if number == 3501
                    else "!"
                    + "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(128))
                    if number == 3502
                    else f"Facility {number % 7}",
                    Decimal("100.250000001"),
                    None if number % 7 == 0 else Decimal(number % 5).scaleb(-9),
                    Decimal("10.000000000"),
                    "[]",
                    f"{number:064x}",
                )
            )
        db.executemany("INSERT INTO source VALUES (?,?,?,?,?,?,?,?,?)", rows)
        db.execute("COPY source TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(path)])
    manifest = {
        "source_snapshot_id": "a" * 32,
        "datasets": {
            dataset: {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        },
    }
    return ModelBundle("b" * 32, directory, manifest)


@pytest.fixture
def browse_client(authenticated_client, browse_bundle, monkeypatch):
    client, user = authenticated_client("analyst")
    monkeypatch.setattr(api_services, "_bundle", lambda _: browse_bundle)
    return client, user


def browse(client, payload, *, expected=200):
    result = client.post(
        "/api/v1/data/generator_daily", json.dumps(payload), content_type="application/json"
    )
    assert result.status_code == expected, result.content
    return result.json()


def test_global_search_matches_beyond_first_thousand_and_treats_wildcards_literally(browse_client):
    client, _ = browse_client
    result = browse(client, {"search": "o'BRIEN %_ ω nEeDlE", "limit": 25})
    names = [c["name"] for c in result["columns"]]
    assert result["returned_rows"] == 1 and result["next_cursor"] is None
    assert result["rows"][0][names.index("generator_id")] == "03501"
    assert result["rows"][0][names.index("capacity_mw")] == "100.250000001"
    assert result["query"]["search"] == "o'BRIEN %_ ω nEeDlE"
    assert browse(client, {"search": "%' OR 1=1 --"})["rows"] == []


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_seek_traversal_covers_all_rows_with_duplicate_decimal_sorts_and_nulls(
    browse_client, direction
):
    client, _ = browse_client
    request = {"sort": "outage_mw", "direction": direction, "limit": 997}
    collected = []
    while True:
        result = browse(client, request)
        assert len(result["rows"]) <= 997
        collected.extend(result["rows"])
        if not result["next_cursor"]:
            break
        request["cursor"] = result["next_cursor"]
    names = [c["name"] for c in result["columns"]]
    identifiers = [row[names.index("generator_id")] for row in collected]
    assert len(identifiers) == len(set(identifiers)) == 5005
    expected = sorted(
        collected,
        key=lambda r: (
            r[5] is None,
            (Decimal(r[5]) if direction == "asc" else -Decimal(r[5])) if r[5] is not None else 0,
            r[0],
            r[1],
            r[2],
        ),
    )
    assert collected == expected


def test_typed_conditions_combine_with_exact_ids_dates_and_search(browse_client):
    client, _ = browse_client
    request = {
        "search": "Facility",
        "start": "2026-09-01",
        "end": "2026-09-01",
        "facility_id": "001",
        "generator_id": "00003",
        "sort": "capacity_mw",
        "direction": "desc",
        "filters": [
            {
                "column": "capacity_mw",
                "op": "between",
                "value": "100.250000001",
                "upper": "100.250000001",
            },
            {"column": "outage_mw", "op": "not_null"},
        ],
    }
    result = browse(client, request)
    assert result["returned_rows"] == 1 and result["rows"][0][2] == "00003"
    request["filters"][1] = {"column": "outage_mw", "op": "is_null"}
    assert browse(client, request)["returned_rows"] == 0


def test_long_exact_text_seek_uses_post_without_truncation_and_get_fails_clearly(browse_client):
    client, _ = browse_client
    request = {"sort": "facility_name", "limit": 1}
    packet = browse(client, request)
    assert packet["rows"][0][2] == "03502"
    assert 4096 < len(packet["next_cursor"]) <= 16384
    last = signing.loads(packet["next_cursor"], salt=api_services.CURSOR_SALT)["last"]
    assert len(last[0]) == 8193
    following = browse(client, {**request, "cursor": packet["next_cursor"]})
    assert following["rows"][0][2] != "03502"
    failed = client.get("/api/v1/data/generator_daily", request)
    assert failed.status_code == 413 and failed.json()["code"] == "browse_cursor_too_large"


@pytest.mark.parametrize(
    "changed",
    [
        {"search": "x"},
        {"direction": "desc"},
        {"sort": "capacity_mw"},
        {"limit": 26},
        {"facility_id": "002"},
        {"filters": [{"column": "capacity_mw", "op": "not_null"}]},
        {"model_id": "c" * 32},
    ],
)
def test_cursor_rejects_changed_query_before_native_io(browse_client, monkeypatch, changed):
    client, _ = browse_client
    base = {"limit": 25}
    cursor = browse(client, base)["next_cursor"]
    monkeypatch.setattr(
        api_services, "_run_browse", lambda *a: pytest.fail("Mismatched cursor executed")
    )
    result = browse(client, {**base, **changed, "cursor": cursor}, expected=400)
    assert result["code"] == "invalid_cursor"


def test_cursor_normalization_accepts_equivalent_predicate_order(browse_client):
    client, _ = browse_client
    first = {"column": "capacity_mw", "op": "gte", "value": "100.25"}
    second = {"column": "outage_mw", "op": "not_null"}
    request = {"limit": 25, "filters": [first, second]}
    page = browse(client, request)
    following = browse(
        client, {**request, "filters": [second, first, first], "cursor": page["next_cursor"]}
    )
    assert {row[2] for row in page["rows"]}.isdisjoint({row[2] for row in following["rows"]})


def test_delivery_rechecks_current_role_after_native_execution(browse_client, monkeypatch):
    client, user = browse_client
    original = api_services.run_browse

    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        Persona.objects.filter(user=user).update(role="viewer")
        return result

    monkeypatch.setattr(api_services, "run_browse", revoke)
    result = browse(client, {}, expected=403)
    assert result["code"] == "permissions_changed" and "rows" not in result


def test_post_requires_csrf_and_rejects_duplicate_unknown_or_sql_fields(browse_client):
    client, _ = browse_client
    for raw in [
        '{"search":"x","search":"y"}',
        '{"sql":"SELECT * FROM generator_daily"}',
        '{"filters":[{"column":"facility_id","op":"eq","value":"001","sql":"COPY x"}]}',
    ]:
        result = client.post("/api/v1/data/generator_daily", raw, content_type="application/json")
        assert result.status_code == 400
    token = client.defaults.pop("HTTP_X_CSRFTOKEN")
    assert (
        client.post(
            "/api/v1/data/generator_daily", "{}", content_type="application/json"
        ).status_code
        == 403
    )
    client.defaults["HTTP_X_CSRFTOKEN"] = token


def test_signed_but_malformed_or_expired_seek_is_not_executed(browse_client, monkeypatch):
    client, _ = browse_client
    packet = browse(client, {"limit": 1})
    token = signing.loads(packet["next_cursor"], salt=api_services.CURSOR_SALT)
    monkeypatch.setattr(
        api_services, "_run_browse", lambda *a: pytest.fail("Invalid seek executed")
    )
    for last in [None, [], ["bad-date", "001", "00000"], ["2026-09-01", {"id": "001"}, "00000"]]:
        bad = api_services._cursor({**token, "last": last})
        assert browse(client, {"limit": 1, "cursor": bad}, expected=400)["code"] == "invalid_cursor"
