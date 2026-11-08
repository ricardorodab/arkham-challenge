"""Fetch the fixed EIA nuclear-outage routes with bounded, secret-safe I/O.

The endpoint is deliberately not configurable because requests carry an API key.
Responses are sanitized before leaving the client, including echoed request
parameters. Pagination checks a live source rather than creating an upstream
transaction; row/domain validation belongs to ``domain`` and ``pipeline``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE_URL = "https://api.eia.gov/v2/nuclear-outages/"
ROUTE_KEYS: dict[str, tuple[str, ...]] = {
    "us-nuclear-outages": ("period",),
    "facility-nuclear-outages": ("period", "facility"),
    "generator-nuclear-outages": ("period", "facility", "generator"),
}
REQUIRED_METRICS = ("capacity", "outage", "percentOutage")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REDACTED = "[REDACTED]"
_SECRET_FIELDS = {
    "api_key",
    "apikey",
    "eia_api_key",
    "authorization",
    "cookie",
    "set-cookie",
}
_QUERY_SECRET = re.compile(
    r"([?&](?:api_key|apikey|eia_api_key)=)[^&#\s\"'<>]*",
    re.IGNORECASE,
)
_LOG = logging.getLogger(__name__)


def sanitize(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Return recursively scrubbed JSON-like data without mutating the input.

    Redact credential-named fields, query-string secrets and supplied literal or
    URL-encoded canaries, including mapping keys. Tuples become lists. Other scalar
    values pass through; this is a credential scrubber, not general JSON validation.
    """
    if isinstance(value, Mapping):
        return {
            _sanitize_text(str(key), secrets): REDACTED
            if str(key).lower() in _SECRET_FIELDS
            else sanitize(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item, secrets) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value, secrets)
    return value


def _sanitize_text(value: str, secrets: tuple[str, ...]) -> str:
    """Redact secret query parameters and explicit raw/encoded secret strings.

    Empty canaries are ignored to avoid replacing every character boundary. Matching
    credential parameter names is case-insensitive; literal secret replacement is
    exact and does not infer unknown credentials.
    """
    cleaned = _QUERY_SECRET.sub(lambda match: match.group(1) + REDACTED, value)
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, REDACTED)
            cleaned = cleaned.replace(quote(secret, safe=""), REDACTED)
    return cleaned


class EIAClientError(Exception):
    """Represent a public source failure with explicit retry metadata.

    Messages supplied by this module exclude URLs and response bodies. ``code`` is
    stable for callers; optional status, delay and UTC retry time describe recovery
    without exposing the request or the credential.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_at: datetime | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the caller-safe message, machine code and optional retry hints.

        This constructor does not sanitize arbitrary caller-provided text. ``retryable``
        indicates whether another attempt may help, not that this client will exceed its
        configured attempt or waiting limits.
        """
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_at = retry_at
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass(frozen=True)
class RouteMetadata:
    """Hold validated route coverage and the sanitized metadata response.

    Start/end are advertised observation dates, not extraction timestamps or a
    completeness guarantee. Required frequency, measures, units and facets have been
    checked by ``fetch_metadata``; ``raw`` remains available for provenance.
    """

    route: str
    start_period: date
    end_period: date
    data_fields: dict[str, dict[str, Any]]
    raw: dict[str, Any]
    frequencies: tuple[str, ...] = ("daily",)
    facets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Page:
    """Carry one nonempty response page and its sanitized provenance.

    ``offset`` is the requested row offset and ``declared_total`` is diagnostic only.
    ``metadata`` excludes response data rows; the pipeline can combine it with
    ``rows`` for optional sanitized recordings.
    """

    rows: list[dict[str, Any]]
    declared_total: int | None
    offset: int
    metadata: dict[str, Any] = field(default_factory=dict)


class _NoRedirect(HTTPRedirectHandler):
    """Prevent urllib from forwarding a query-string API key on redirect."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        """Refuse redirect construction so the original response becomes an error."""
        # A redirected request must not disclose the query-string API key.
        return None


class EIAClient:
    """Coordinate bounded requests and ordered pagination across route workers.

    One instance shares a request-start limiter, 429 cooldown and statistics across
    threads. Each route's pages remain sequential. Injectable transport, clocks,
    sleep and jitter permit deterministic offline tests. Per-request limits do not
    establish a strict whole-ingestion deadline or a source snapshot transaction.
    """

    def __init__(
        self,
        api_key: str,
        *,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
        max_retries: int = 3,
        max_retry_after: float = 60.0,
        min_request_interval: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        jitter: Callable[[], float] | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_pages: int = 100_000,
    ) -> None:
        """Validate request limits and retain the private key and injectable I/O.

        ``max_retries`` counts attempts after the first. ``max_retry_after`` bounds waits,
        ``min_request_interval`` spaces starts across workers, and response/page limits
        bound each transport or pagination operation. Raise ``EIAClientError`` with code
        ``configuration`` for an empty key or invalid limits. Construction makes no HTTP
        request; the default opener refuses redirects.
        """
        if not isinstance(api_key, str) or not api_key.strip():
            raise EIAClientError("configuration", "EIA_API_KEY is required.")
        if (
            timeout <= 0
            or max_retries < 0
            or max_retry_after < 0
            or min_request_interval < 0
            or max_response_bytes <= 0
            or max_pages <= 0
        ):
            raise EIAClientError("configuration", "Invalid HTTP client limits.")
        self._api_key = api_key.strip()
        self._opener = opener or build_opener(_NoRedirect()).open
        self._sleep = sleep
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_retry_after = max_retry_after
        self._min_request_interval = min_request_interval
        self._monotonic = monotonic
        self._now = now or (lambda: datetime.now(UTC))
        self._jitter = jitter or (lambda: random.uniform(0.0, 0.25))
        self._max_response_bytes = max_response_bytes
        self._max_pages = max_pages
        self._last_request_start: float | None = None
        self._cooldown_until = 0.0
        self._cooldown_retry_at: datetime | None = None
        self._rate_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self.requests = 0
        self.retries = 0
        self.pages = 0
        self.declared_totals: dict[str, int | None] = {}

    def sanitize(self, value: Any) -> Any:
        """Scrub a response using both known field names and this client's key."""
        return sanitize(value, (self._api_key,))

    @property
    def statistics(self) -> dict[str, Any]:
        """Return a lock-protected copy of request/retry/page/declared-total counts.

        Pages count nonempty pages, whereas requests include metadata, terminal empty
        pages and retries. The returned mappings can be persisted without exposing the
        client's private key.
        """
        with self._counter_lock:
            return {
                "requests": self.requests,
                "retries": self.retries,
                "pages": self.pages,
                "declared_totals": dict(self.declared_totals),
            }

    def fetch_metadata(self, route: str) -> RouteMetadata:
        """Fetch and validate the daily metadata contract for an allowlisted route.

        Return coverage dates, measures, sanitized raw JSON, frequencies and facets.
        Raise ``EIAClientError`` for unsupported routes, transport failures or incompatible
        metadata; no observation pages are fetched by this method.
        """
        self._check_route(route)
        payload = self._request_json(route, data=False, params={})
        response = payload.get("response")
        if not isinstance(response, dict):
            raise EIAClientError("invalid_metadata", "EIA metadata has no response object.")
        try:
            start = self._parse_period(response["startPeriod"])
            end = self._parse_period(response["endPeriod"])
            frequencies = tuple(item["id"] for item in response["frequency"])
            fields = response["data"]
            if (
                start > end
                or "daily" not in frequencies
                or not isinstance(fields, dict)
                or any(not isinstance(fields.get(name), dict) for name in REQUIRED_METRICS)
            ):
                raise ValueError
            expected_units = {
                "capacity": "megawatts",
                "outage": "megawatts",
                "percentOutage": "percent",
            }
            if any(fields[name].get("units") != units for name, units in expected_units.items()):
                raise ValueError
            facets = tuple(item["id"] for item in response.get("facets", []))
            required_facets = set(ROUTE_KEYS[route]) - {"period"}
            if not required_facets.issubset(facets):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise EIAClientError(
                "invalid_metadata",
                "EIA metadata does not match the daily nuclear-outage contract.",
            ) from None
        return RouteMetadata(route, start, end, fields, payload, frequencies, facets)

    def iter_pages(
        self, route: str, start: date, end: date, page_size: int = 5000
    ) -> Iterator[Page]:
        """Request nonempty pages sorted by complete natural key until an empty page.

        Request only the required measures within inclusive dates. Advance by rows
        actually received and continue after short pages; ``response.total`` is recorded
        but cannot terminate extraction. Repeated content/boundaries, invalid page shape
        or the page cap raise ``EIAClientError``. Row values are validated later by the
        pipeline. Exhausting this iterator makes a final empty-page request and updates
        shared statistics; EIA may still change while pages are being read.
        """
        self._check_route(route)
        if (
            not isinstance(start, date)
            or not isinstance(end, date)
            or isinstance(start, datetime)
            or isinstance(end, datetime)
            or start > end
            or isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 5000
        ):
            raise EIAClientError("configuration", "Invalid extraction dates or page size.")
        offset = 0
        seen_hashes: set[str] = set()
        seen_boundaries: set[tuple[tuple[Any, ...], tuple[Any, ...]]] = set()
        for _ in range(self._max_pages):
            params: dict[str, Any] = {
                "frequency": "daily",
                "data[]": list(REQUIRED_METRICS),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "offset": offset,
                "length": page_size,
            }
            for index, key in enumerate(ROUTE_KEYS[route]):
                params[f"sort[{index}][column]"] = key
                params[f"sort[{index}][direction]"] = "asc"
            payload = self._request_json(route, data=True, params=params)
            response = payload.get("response")
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise EIAClientError(
                    "invalid_response", "EIA data response must contain a row list."
                )
            rows = response["data"]
            if len(rows) > page_size or any(not isinstance(row, dict) for row in rows):
                raise EIAClientError(
                    "invalid_response", "EIA returned an invalid or oversized row page."
                )
            declared_total = self._parse_total(response.get("total"))
            with self._counter_lock:
                self.declared_totals[route] = declared_total
            if not rows:
                return
            page_hash = hashlib.sha256(
                json.dumps(
                    rows,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            ).hexdigest()
            boundaries = self._boundaries(rows, ROUTE_KEYS[route])
            if page_hash in seen_hashes or (
                boundaries is not None and boundaries in seen_boundaries
            ):
                raise EIAClientError(
                    "pagination_error", "EIA pagination repeated a page or made no progress."
                )
            seen_hashes.add(page_hash)
            if boundaries is not None:
                seen_boundaries.add(boundaries)
            metadata = {key: value for key, value in payload.items() if key != "response"}
            metadata["response"] = {key: value for key, value in response.items() if key != "data"}
            with self._counter_lock:
                self.pages += 1
            _LOG.info(
                "eia_page",
                extra={
                    "route": route,
                    "offset": offset,
                    "rows": len(rows),
                    "declared_total": declared_total,
                },
            )
            yield Page(rows, declared_total, offset, metadata)
            offset += len(rows)
        raise EIAClientError(
            "pagination_error", "EIA pagination exceeded the configured safety limit."
        )

    @staticmethod
    def _check_route(route: str) -> None:
        """Reject any route outside the fixed nuclear-outage allowlist."""
        if route not in ROUTE_KEYS:
            raise EIAClientError("configuration", "Unknown nuclear-outage route.")

    @staticmethod
    def _parse_period(value: Any) -> date:
        """Parse a metadata date with exact lexical and calendar validation.

        Raise ``ValueError`` on invalid input so ``fetch_metadata`` can normalize it to a
        safe ``invalid_metadata`` error.
        """
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)

    @staticmethod
    def _parse_total(value: Any) -> int | None:
        """Return a nonnegative integer total, or None for unusable metadata.

        Accept integer values and ASCII digit strings, explicitly excluding booleans.
        The result is provenance rather than a completeness or termination condition.
        """
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, str) and value.isascii() and value.isdigit():
            return int(value)
        return None

    @staticmethod
    def _boundaries(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> Any:
        """Return comparable first/last natural-key tuples for a nonempty page.

        Return None when a boundary has unsupported or missing scalar types. This guard
        supplements full-page hashes; it does not validate rows or prove source ordering.
        Callers must supply a nonempty ``rows`` list.
        """
        values = tuple(tuple(row.get(key) for key in keys) for row in (rows[0], rows[-1]))
        if any(
            not isinstance(value, (str, int, float)) or isinstance(value, bool)
            for boundary in values
            for value in boundary
        ):
            return None
        return values

    def _request_json(self, route: str, *, data: bool, params: dict[str, Any]) -> dict[str, Any]:
        """Issue one logical fixed-route request with bounded retry and shared pacing.

        Expand list-valued query parameters, count each attempt and log only safe route
        metadata. Retry eligible failures with backoff/jitter or Retry-After; 429 waits
        extend the shared cooldown. Propagate ``EIAClientError`` when permanent, exhausted
        or too delayed. ``route`` and ``params`` are internal validated request inputs.
        """
        query = {"api_key": self._api_key, **params}
        url = BASE_URL + route + ("/data/" if data else "/") + "?" + urlencode(query, doseq=True)
        request = Request(
            url, headers={"Accept": "application/json", "User-Agent": "outage-explorer/0.1"}
        )
        for attempt in range(self._max_retries + 1):
            self._acquire_request_slot()
            with self._counter_lock:
                self.requests += 1
            _LOG.info(
                "eia_request",
                extra={"route": route, "offset": params.get("offset"), "attempt": attempt + 1},
            )
            try:
                return self._one_request(request)
            except EIAClientError as error:
                wait = error.retry_after
                if wait is None:
                    wait = min(2.0**attempt, 30.0) + self._jitter()
                if error.code == "rate_limited":
                    with self._rate_lock:
                        self._cooldown_until = max(self._cooldown_until, self._monotonic() + wait)
                        if error.retry_at is not None:
                            self._cooldown_retry_at = error.retry_at
                if not error.retryable or attempt >= self._max_retries:
                    raise
                if wait > self._max_retry_after:
                    raise
                with self._counter_lock:
                    self.retries += 1
                _LOG.warning(
                    "eia_retry",
                    extra={"route": route, "error_code": error.code, "delay_seconds": wait},
                )
                if error.code != "rate_limited":
                    self._sleep(wait)
        raise AssertionError("unreachable")

    def _acquire_request_slot(self) -> None:
        """Reserve a globally spaced request start and honor shared 429 cooldown.

        Hold the rate lock while computing/sleeping so route threads cannot bypass the
        gate; release it before network I/O. Excessive remaining cooldown raises a
        retryable ``rate_limited`` error carrying the recorded retry time.
        """
        # A single lock spaces starts across all route threads, including retries.
        # No HTTP request executes while this lock is held.
        with self._rate_lock:
            current = self._monotonic()
            cooldown = max(0.0, self._cooldown_until - current)
            if cooldown > self._max_retry_after:
                raise EIAClientError(
                    "rate_limited",
                    "EIA rate limit reached; retry later.",
                    retryable=True,
                    status_code=429,
                    retry_at=self._cooldown_retry_at,
                    retry_after=cooldown,
                )
            interval_wait = 0.0
            if self._last_request_start is not None:
                interval_wait = max(
                    0.0, self._min_request_interval - (current - self._last_request_start)
                )
            pause = max(cooldown, interval_wait)
            if pause:
                self._sleep(pause)
            self._last_request_start = self._monotonic()

    def _one_request(self, request: Request) -> dict[str, Any]:
        """Read one size-bounded JSON object and return its sanitized contents.

        Close responses, normalize HTTP/network failures and recognize error payloads
        returned with HTTP 200. Reject oversized bytes, invalid encoding/JSON, non-object
        payloads and unsupported nesting as safe ``EIAClientError`` values. No retry or
        request scheduling happens here; those are owned by ``_request_json``.
        """
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = getattr(response, "status", 200)
                if status >= 300:
                    raise self._http_error(status, response.headers.get("Retry-After"))
                content = response.read(self._max_response_bytes + 1)
        except HTTPError as error:
            status = error.code
            retry_after = error.headers.get("Retry-After") if error.headers else None
            error.close()
            raise self._http_error(status, retry_after) from None
        except (URLError, TimeoutError, ConnectionError, OSError):
            raise EIAClientError(
                "network_error",
                "EIA could not be reached within the request limits.",
                retryable=True,
            ) from None
        if len(content) > self._max_response_bytes:
            raise EIAClientError(
                "response_too_large", "EIA response exceeded the configured byte limit."
            )
        try:
            payload = json.loads(content.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise EIAClientError("invalid_json", "EIA returned invalid JSON.") from None
        if not isinstance(payload, dict):
            raise EIAClientError("invalid_response", "EIA JSON response must be an object.")
        if payload.get("error"):
            status = self._embedded_status(payload)
            if status is not None:
                raise self._http_error(status, None)
            raise EIAClientError("source_error", "EIA reported a source error.")
        try:
            return self.sanitize(payload)
        except RecursionError:
            raise EIAClientError(
                "invalid_response", "EIA response exceeded the supported nesting depth."
            ) from None

    @staticmethod
    def _embedded_status(payload: dict[str, Any]) -> int | None:
        """Map recognized HTTP-like or named source errors to a status code.

        Return None for unknown codes so the caller emits a generic safe source error
        rather than relaying provider text.
        """
        error = payload.get("error")
        value = error.get("code") if isinstance(error, dict) else payload.get("code")
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            status = int(value)
            return status if 400 <= status <= 599 else None
        if isinstance(value, str) and value in {
            "API_KEY_INVALID",
            "API_KEY_MISSING",
            "API_KEY_DISABLED",
            "API_KEY_UNVERIFIED",
        }:
            return 403
        if isinstance(value, str) and value in {"OVER_RATE_LIMIT", "API_KEY_OVER_RATE_LIMIT"}:
            return 429
        return None

    def _http_error(self, status: int, retry_after_header: str | None) -> EIAClientError:
        """Construct a safe error and retry policy for an HTTP status.

        401/403 are permanent credential failures; 429 and 5xx are retryable and retain
        parsed Retry-After hints. Other statuses are permanent request failures. This
        method returns an exception object without raising or sleeping.
        """
        delay, retry_at = self._retry_after(retry_after_header)
        if status in {401, 403}:
            return EIAClientError(
                "bad_credentials", "EIA rejected the API credentials.", status_code=status
            )
        if status == 429:
            return EIAClientError(
                "rate_limited",
                "EIA rate limit reached; retry later.",
                retryable=True,
                status_code=status,
                retry_after=delay,
                retry_at=retry_at,
            )
        if 500 <= status <= 599:
            return EIAClientError(
                "upstream_unavailable",
                "EIA is temporarily unavailable.",
                retryable=True,
                status_code=status,
                retry_after=delay,
                retry_at=retry_at,
            )
        return EIAClientError(
            "http_error", f"EIA rejected the request (HTTP {status}).", status_code=status
        )

    def _retry_after(self, value: str | None) -> tuple[float | None, datetime | None]:
        """Parse delta seconds or an HTTP date into delay and UTC retry time.

        Clamp past dates to zero wait. Missing or malformed headers return (None, None),
        allowing backoff fallback. Preserve an excessive numeric delay even if its date
        cannot fit ``datetime`` so the caller can refuse the wait rather than retry early.
        """
        if not value:
            return None, None
        current = self._now()
        try:
            if re.fullmatch(r"\d+", value.strip()):
                delay = float(value.strip())
                try:
                    retry_at = current + timedelta(seconds=delay)
                except OverflowError:
                    # Still refuse an unreasonably long wait, even when its
                    # corresponding date cannot be represented by datetime.
                    retry_at = None
                return delay, retry_at
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            retry_at = retry_at.astimezone(UTC)
            return max(0.0, (retry_at - current).total_seconds()), retry_at
        except (TypeError, ValueError, OverflowError):
            return None, None
