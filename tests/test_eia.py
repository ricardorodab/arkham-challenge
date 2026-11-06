from __future__ import annotations

import io
import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from outage_explorer.eia import REDACTED, EIAClient, EIAClientError, sanitize

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "TEST_ONLY_NEVER_A_REAL_EIA_KEY"
START = date(2026, 9, 1)
END = date(2026, 9, 30)
ROUTE = "us-nuclear-outages"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Response(io.BytesIO):
    def __init__(self, payload: object, status: int = 200, headers: dict | None = None):
        content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        super().__init__(content)
        self.status = status
        self.headers = headers or {}


class Transport:
    def __init__(self, outcomes: list):
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def __call__(self, request, *, timeout):
        self.calls.append(request.full_url)
        assert timeout > 0
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return Response(result)


class Clock:
    def __init__(self):
        self.current = 0.0
        self.sleeps: list[float] = []
        self.lock = threading.Lock()

    def monotonic(self):
        with self.lock:
            return self.current

    def sleep(self, delay):
        with self.lock:
            self.sleeps.append(delay)
            self.current += delay


def make_client(outcomes: list, **kwargs):
    transport = Transport(outcomes)
    clock = Clock()
    options = {
        "min_request_interval": 0,
        "max_retries": 0,
        "jitter": lambda: 0,
        "sleep": clock.sleep,
        "monotonic": clock.monotonic,
    }
    options.update(kwargs)
    return EIAClient(KEY, opener=transport, **options), transport, clock


def http_error(status: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(
        f"https://api.eia.gov/data/?api_key={KEY}",
        status,
        f"unsafe upstream message {KEY}",
        headers,
        None,
    )


def test_metadata_validates_daily_fields_units_and_removes_echoed_key():
    payload = fixture("eia_metadata.json")
    payload["request"]["params"]["api_key"] = KEY
    client, transport, _ = make_client([payload])
    metadata = client.fetch_metadata("facility-nuclear-outages")
    assert metadata.start_period == date(2007, 1, 1)
    assert metadata.end_period == END
    assert metadata.data_fields["capacity"]["units"] == "megawatts"
    assert metadata.facets == ("facility",)
    assert KEY not in json.dumps(metadata.raw)
    assert metadata.raw["request"]["params"]["api_key"] == REDACTED
    assert "/facility-nuclear-outages/?" in transport.calls[0]


@pytest.mark.parametrize(
    "mutation", ["missing_metric", "wrong_units", "bad_date", "not_daily", "missing_facet"]
)
def test_metadata_contract_breaks_are_rejected(mutation):
    payload = fixture("eia_metadata.json")
    response = payload["response"]
    if mutation == "missing_metric":
        response["data"].pop("outage")
    elif mutation == "wrong_units":
        response["data"]["capacity"]["units"] = "kilowatts"
    elif mutation == "bad_date":
        response["startPeriod"] = "2026-02-30"
    elif mutation == "not_daily":
        response["frequency"] = [{"id": "monthly"}]
    else:
        response["facets"] = []
    client, _, _ = make_client([payload])
    with pytest.raises(EIAClientError, match="metadata") as caught:
        client.fetch_metadata("facility-nuclear-outages")
    assert caught.value.code == "invalid_metadata"


def test_short_page_and_wrong_total_still_require_an_empty_terminal_page():
    first = fixture("eia_national_page.json")
    second = fixture("eia_national_page.json")
    second["response"]["data"][0]["period"] = "2026-09-02"
    client, transport, _ = make_client([first, second, fixture("eia_empty_page.json")])
    pages = list(client.iter_pages(ROUTE, START, END))
    assert [page.offset for page in pages] == [0, 1]
    assert [page.declared_total for page in pages] == [2850, 2850]
    assert [parse_qs(urlsplit(url).query)["offset"] for url in transport.calls] == [
        ["0"],
        ["1"],
        ["2"],
    ]
    assert client.statistics == {
        "requests": 3,
        "retries": 0,
        "pages": 2,
        "declared_totals": {ROUTE: 2850},
    }


def test_stable_sort_includes_full_generator_key_and_three_metrics():
    client, transport, _ = make_client([fixture("eia_empty_page.json")])
    assert list(client.iter_pages("generator-nuclear-outages", START, END)) == []
    query = parse_qs(urlsplit(transport.calls[0]).query)
    assert [query[f"sort[{i}][column]"][0] for i in range(3)] == ["period", "facility", "generator"]
    assert query["data[]"] == ["capacity", "outage", "percentOutage"]
    assert all(query[f"sort[{i}][direction]"] == ["asc"] for i in range(3))


@pytest.mark.parametrize("changed_values", [False, True])
def test_repeated_or_nonprogressing_page_is_rejected(changed_values):
    first = fixture("eia_national_page.json")
    second = fixture("eia_national_page.json")
    if changed_values:
        second["response"]["data"][0]["outage"] = "9999"
    client, _, _ = make_client([first, second])
    pages = client.iter_pages(ROUTE, START, END)
    next(pages)
    with pytest.raises(EIAClientError) as caught:
        next(pages)
    assert caught.value.code == "pagination_error"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 302])
def test_permanent_http_errors_never_retry_or_expose_transport_messages(status):
    client, transport, clock = make_client([http_error(status)], max_retries=3)
    with pytest.raises(EIAClientError) as caught:
        client.fetch_metadata(ROUTE)
    assert caught.value.status_code == status
    assert not caught.value.retryable
    assert client.retries == 0
    assert len(transport.calls) == 1
    assert clock.sleeps == []
    assert KEY not in "".join(traceback.format_exception(caught.value))


def test_embedded_invalid_key_error_is_rejected_even_with_http_200():
    client, _, _ = make_client([fixture("eia_invalid_key.json")])
    with pytest.raises(EIAClientError) as caught:
        client.fetch_metadata(ROUTE)
    assert caught.value.code == "bad_credentials"


def test_429_respects_retry_after_before_retrying():
    client, transport, clock = make_client(
        [http_error(429, "2"), fixture("eia_metadata.json")],
        max_retries=1,
    )
    client.fetch_metadata("facility-nuclear-outages")
    assert clock.sleeps == [2.0]
    assert len(transport.calls) == 2
    assert client.retries == 1


def test_http_date_retry_after_is_supported():
    current = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
    client, _, clock = make_client(
        [http_error(503, "Tue, 01 Sep 2026 00:00:03 GMT"), fixture("eia_metadata.json")],
        max_retries=1,
        now=lambda: current,
    )
    client.fetch_metadata("facility-nuclear-outages")
    assert clock.sleeps == [3.0]


def test_long_retry_after_returns_retry_time_without_blocking_or_further_requests():
    current = datetime(2026, 9, 1, tzinfo=UTC)
    client, transport, clock = make_client(
        [http_error(429, "85809")], max_retries=3, now=lambda: current
    )
    with pytest.raises(EIAClientError) as caught:
        client.fetch_metadata(ROUTE)
    assert caught.value.code == "rate_limited"
    assert caught.value.retryable
    assert caught.value.retry_at.isoformat() == "2026-09-01T23:50:09+00:00"
    assert clock.sleeps == []
    with pytest.raises(EIAClientError) as next_caught:
        client.fetch_metadata(ROUTE)
    assert next_caught.value.code == "rate_limited"
    assert len(transport.calls) == 1


def test_unrepresentably_long_retry_after_is_not_ignored():
    client, transport, clock = make_client([http_error(429, "9" * 400)], max_retries=3)
    with pytest.raises(EIAClientError) as caught:
        client.fetch_metadata(ROUTE)
    assert caught.value.code == "rate_limited"
    assert caught.value.retry_after > 60
    assert caught.value.retry_at is None
    assert len(transport.calls) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("failure", [http_error(500), URLError("unsafe " + KEY), TimeoutError(KEY)])
def test_transient_failures_retry_with_bounded_backoff(failure):
    client, transport, clock = make_client([failure, fixture("eia_metadata.json")], max_retries=1)
    client.fetch_metadata("facility-nuclear-outages")
    assert len(transport.calls) == 2
    assert clock.sleeps == [1.0]
    assert client.retries == 1


def test_retry_exhaustion_reports_safe_error():
    client, transport, clock = make_client([URLError(KEY)] * 3, max_retries=2)
    with pytest.raises(EIAClientError) as caught:
        client.fetch_metadata(ROUTE)
    assert caught.value.code == "network_error"
    assert len(transport.calls) == 3
    assert clock.sleeps == [1.0, 2.0]
    assert KEY not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "payload,expected",
    [
        (b"not JSON", "invalid_json"),
        ([1, 2], "invalid_response"),
        ({"response": {}}, "invalid_response"),
        ({"response": {"data": [42]}}, "invalid_response"),
    ],
)
def test_malformed_data_is_rejected_without_retry(payload, expected):
    client, transport, _ = make_client([payload], max_retries=2)
    with pytest.raises(EIAClientError) as caught:
        list(client.iter_pages(ROUTE, START, END))
    assert caught.value.code == expected
    assert len(transport.calls) == 1


def test_response_byte_limit_is_enforced():
    client, _, _ = make_client([b"x" * 101], max_response_bytes=100)
    with pytest.raises(EIAClientError) as caught:
        client.fetch_metadata(ROUTE)
    assert caught.value.code == "response_too_large"


def test_nested_secrets_and_credential_urls_are_sanitized():
    payload = {
        "request": {"params": {"api_key": KEY}},
        "nested": [{"Authorization": KEY}, f"https://eia.gov/?api_key={KEY}&offset=1"],
        "message": f"Server echoed {KEY}",
    }
    cleaned = sanitize(payload, (KEY,))
    assert KEY not in json.dumps(cleaned)
    assert cleaned["request"]["params"]["api_key"] == REDACTED
    assert "offset=1" in cleaned["nested"][1]


def test_secret_canaries_in_mapping_keys_are_sanitized_recursively():
    payload = {
        KEY: "unexpected field name",
        "nested": {f"prefix-{KEY}-suffix": {"api_key": KEY}},
        f"https://eia.gov/?api_key={KEY}&offset=1": "URL used as a key",
    }
    cleaned = sanitize(payload, (KEY,))
    assert KEY not in json.dumps(cleaned)
    assert cleaned[REDACTED] == "unexpected field name"
    assert cleaned["nested"][f"prefix-{REDACTED}-suffix"]["api_key"] == REDACTED
    assert cleaned[f"https://eia.gov/?api_key={REDACTED}&offset=1"] == "URL used as a key"


def test_parallel_routes_share_one_request_rate_limiter_and_counters():
    clock = Clock()
    request_clock = threading.local()
    reservations = []
    observations = []
    calls_lock = threading.Lock()
    # The timeout is a deadlock watchdog, not a request-speed assertion.
    transports_ready = threading.Barrier(3, timeout=5)

    def monotonic():
        instant = clock.monotonic()
        request_clock.last_read = instant
        return instant

    def opener(request, *, timeout):
        # The client reads its reservation time while holding the shared rate
        # lock. A different route can advance fake time before this opener runs.
        reserved_at = request_clock.last_read
        # Require transport overlap: the rate lock must be released before I/O.
        transports_ready.wait()
        with calls_lock:
            reservations.append(reserved_at)
            observations.append(clock.monotonic())
        return Response(fixture("eia_empty_page.json"))

    client = EIAClient(
        KEY, opener=opener, min_request_interval=1.0, sleep=clock.sleep, monotonic=monotonic
    )
    routes = [ROUTE, "facility-nuclear-outages", "generator-nuclear-outages"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda route: list(client.iter_pages(route, START, END)), routes))
    assert results == [[], [], []]
    assert sorted(reservations) == [0.0, 1.0, 2.0]
    assert clock.sleeps == [1.0, 1.0]
    # All slots are reserved before recording, so the late shared-clock samples
    # intentionally coincide instead of being mistaken for reservation times.
    assert observations == [2.0, 2.0, 2.0]
    assert client.statistics == {
        "requests": 3,
        "retries": 0,
        "pages": 0,
        "declared_totals": {route: 2850 for route in routes},
    }


def test_unknown_routes_are_rejected_before_transport():
    client, transport, _ = make_client([])
    with pytest.raises(EIAClientError) as caught:
        client.fetch_metadata("https://other.example/" + KEY)
    assert caught.value.code == "configuration"
    assert transport.calls == []
    assert KEY not in str(caught.value)
