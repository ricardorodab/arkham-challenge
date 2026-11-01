"""Offline replays of genuine, partial, sanitized EIA capture samples."""

from __future__ import annotations

import hashlib
import io
import json
import traceback
from datetime import date
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from outage_explorer.domain import ROUTES, validate_row
from outage_explorer.eia import REDACTED, EIAClient, EIAClientError

RECORDED = Path(__file__).parent / "fixtures" / "recorded"
START = date(2026, 9, 1)
END = date(2026, 9, 3)
TEST_KEY = "test-key-for-offline-recording-replay"


def load(name):
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


def test_recorded_json_uses_portable_lf_bytes_for_provenance_hashes():
    # Git checkout must not silently change the bytes covered by provenance.
    for source in RECORDED.glob("*.json"):
        raw = source.read_bytes()
        assert b"\r\n" not in raw, source.name
        assert raw.endswith(b"\n"), source.name
        raw.decode("utf-8")


class RecordedResponse(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.status = 200
        self.headers = {}


@pytest.mark.parametrize("spec", ROUTES, ids=lambda spec: spec.name)
def test_recorded_metadata_matches_current_nuclear_outage_contract(spec):
    payload = load(f"{spec.name}_metadata.json")
    calls = []

    def opener(request, *, timeout):
        calls.append(request.full_url)
        return RecordedResponse(payload)

    client = EIAClient(TEST_KEY, opener=opener, min_request_interval=0)
    metadata = client.fetch_metadata(spec.route)
    assert metadata.route == spec.route
    assert metadata.start_period == date(2007, 1, 1)
    assert metadata.end_period >= END
    assert set(metadata.data_fields) >= {"capacity", "outage", "percentOutage"}
    assert "daily" in metadata.frequencies
    assert set(spec.key_fields[1:]).issubset(metadata.facets)
    assert len(calls) == 1


@pytest.mark.parametrize("spec", ROUTES, ids=lambda spec: spec.name)
def test_recorded_first_page_rows_pass_domain_validation(spec):
    payload = load(f"{spec.name}_page.json")
    rows = payload["response"]["data"]
    assert len(rows) == (3 if spec.name == "national" else 50)
    validated = [validate_row(spec, row, START, END) for row in rows]
    assert len({result.key for result in validated}) == len(rows)
    assert payload["request"]["params"].get("api_key") in {None, REDACTED}


@pytest.mark.parametrize("spec", ROUTES, ids=lambda spec: spec.name)
def test_real_first_page_plus_synthetic_terminal_page_exercises_client_protocol(spec):
    """The artificial empty page does not assert complete facility/unit coverage."""
    first_page = load(f"{spec.name}_page.json")
    row_count = len(first_page["response"]["data"])
    synthetic_terminal = {"response": {"total": first_page["response"]["total"], "data": []}}
    responses = iter([first_page, synthetic_terminal])
    calls = []

    def opener(request, *, timeout):
        calls.append(request.full_url)
        return RecordedResponse(next(responses))

    client = EIAClient(TEST_KEY, opener=opener, min_request_interval=0)
    pages = list(client.iter_pages(spec.route, START, END, page_size=50))
    assert len(pages) == 1
    assert pages[0].rows == first_page["response"]["data"]
    assert [parse_qs(urlsplit(url).query)["offset"] for url in calls] == [["0"], [str(row_count)]]
    assert client.statistics["requests"] == 2
    assert TEST_KEY not in json.dumps(pages[0].metadata)


def test_recording_provenance_hashes_and_partial_capture_scope_are_explicit():
    provenance = load("provenance.json")
    assert provenance["capture_local_timezone"] == "America/Los_Angeles"
    assert provenance["capture_local_date"] == "2026-10-31"
    assert provenance["capture_run_finished_at_utc"].startswith("2026-11-01T")
    assert "no successful publication" in provenance["capture_run_result"]
    assert "partial samples" in provenance["scope"]
    for record in provenance["records"]:
        assert (
            hashlib.sha256((RECORDED / record["file"]).read_bytes()).hexdigest() == record["sha256"]
        )


def test_recorded_http_403_is_a_permanent_safe_credential_error():
    recording = load("invalid_credentials.json")
    calls = []

    def opener(request, *, timeout):
        calls.append(request.full_url)
        raise HTTPError(
            request.full_url,
            recording["http_status"],
            "Forbidden",
            Message(),
            io.BytesIO(json.dumps(recording["body"]).encode("utf-8")),
        )

    client = EIAClient(TEST_KEY, opener=opener, min_request_interval=0, max_retries=3)
    with pytest.raises(EIAClientError) as caught:
        client.fetch_metadata("us-nuclear-outages")
    assert caught.value.code == "bad_credentials"
    assert caught.value.status_code == 403
    assert not caught.value.retryable
    assert client.requests == 1
    assert client.retries == 0
    assert len(calls) == 1
    assert TEST_KEY not in "".join(traceback.format_exception(caught.value))
