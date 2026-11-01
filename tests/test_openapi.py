"""Keep the packaged OpenAPI contract aligned with the actual HTTP capabilities."""

from __future__ import annotations

import ast
import inspect
import json
import re
from importlib.resources import files

import pytest
from django.test import Client

from outage_explorer.model_contracts import DATASETS
from outage_explorer.service import api_services, urls, views
from outage_explorer.sql_policy import MAX_ROWS, MAX_SQL_BYTES


@pytest.fixture(scope="module")
def specification():
    return json.loads(
        files("outage_explorer.service").joinpath("openapi.json").read_text(encoding="utf-8")
    )


def walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def resolve(document, reference):
    assert reference.startswith("#/"), "The packaged document must be self-contained."
    value = document
    for token in reference[2:].split("/"):
        value = value[token.replace("~1", "/").replace("~0", "~")]
    return value


def test_all_actual_routes_and_declared_methods_have_exact_contract_coverage(specification):
    assert specification["openapi"] == "3.1.0"
    functions = {
        node.name: node
        for node in ast.parse(inspect.getsource(views)).body
        if isinstance(node, ast.FunctionDef)
    }
    actual = {}
    for route in urls.urlpatterns:
        path = "/" + re.sub(r"<[^:>]+:([^>]+)>", r"{\1}", str(route.pattern))
        function = functions[route.callback.__name__]
        decorator = next(
            item
            for item in function.decorator_list
            if isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "endpoint"
        )
        actual[path] = {argument.value.lower() for argument in decorator.args}
    documented = {path: set(item) for path, item in specification["paths"].items()}
    assert documented == actual
    operations = [
        operation["operationId"]
        for item in specification["paths"].values()
        for operation in item.values()
    ]
    assert len(operations) == len(set(operations)) == 13


def test_all_references_resolve_and_every_operation_has_typed_success_and_safe_errors(
    specification,
):
    for node in walk(specification):
        if isinstance(node, dict) and "$ref" in node:
            assert isinstance(resolve(specification, node["$ref"]), dict)
    for item in specification["paths"].values():
        for operation in item.values():
            assert operation["responses"]["200"]["content"]["application/json"]["schema"]
            for code, response in operation["responses"].items():
                if int(code) < 400:
                    continue
                resolved = resolve(specification, response["$ref"])
                assert resolved["content"]["application/problem+json"]["schema"] == {
                    "$ref": "#/components/schemas/ProblemDetails"
                }
    assert {"401", "403", "405", "409", "413", "415", "422", "429", "503", "504"} <= set(
        specification["components"]["responses"]
    )


def test_session_csrf_public_and_admin_security_requirements_are_explicit(specification):
    assert specification["security"] == [{"SessionCookie": []}]
    schemes = specification["components"]["securitySchemes"]
    assert (schemes["SessionCookie"]["in"], schemes["SessionCookie"]["name"]) == (
        "cookie",
        "sessionid",
    )
    assert (schemes["CSRFHeader"]["in"], schemes["CSRFHeader"]["name"]) == ("header", "X-CSRFToken")
    for path in ("/api/v1/auth/csrf", "/api/v1/health/live"):
        assert specification["paths"][path]["get"]["security"] == []
    for path, item in specification["paths"].items():
        if "post" in item:
            required = set(item["post"]["security"][0])
            assert {"CSRFCookie", "CSRFHeader"} <= required
            assert ("SessionCookie" in required) is (path != "/api/v1/auth/login")
    for path in ("/api/v1/refresh", "/api/v1/refresh/{job_id}", "/api/v1/health/ready"):
        assert all(
            operation["x-allowed-personas"] == ["admin"]
            for operation in specification["paths"][path].values()
        )
    assert specification["paths"]["/api/v1/openapi.json"]["get"]["x-allowed-personas"] == [
        "viewer",
        "analyst",
        "admin",
    ]


def test_contract_does_not_enumerate_forbidden_catalog_metadata(specification):
    encoded = json.dumps(specification)
    for hidden in set(DATASETS) - {"national_daily", "fleet_daily"}:
        assert hidden not in encoded
    dataset = specification["components"]["schemas"]["DatasetId"]
    assert dataset["type"] == "string" and "enum" not in dataset
    assert "D:\\" not in encoded and ".parquet" not in encoded


def test_mutation_schemas_are_strict_and_limits_match_execution_contract(specification, settings):
    for item in specification["paths"].values():
        for operation in item.values():
            if "requestBody" not in operation:
                continue
            body = operation["requestBody"]
            assert body["required"] is True
            schema = resolve(specification, body["content"]["application/json"]["schema"]["$ref"])
            assert schema["type"] == "object" and schema["additionalProperties"] is False
            assert set(schema["required"]) <= set(schema["properties"])
    limits = specification["x-limits"]
    assert limits["query_retained_rows"] == MAX_ROWS == settings.OUTAGE_QUERY_MAX_ROWS
    assert limits["sql_utf8_bytes"] == MAX_SQL_BYTES
    assert limits["response_bytes"] == settings.OUTAGE_QUERY_MAX_BYTES
    assert limits["query_timeout_seconds"] == settings.OUTAGE_QUERY_TIMEOUT_SECONDS
    assert limits["cursor_ttl_seconds"] == 900 and limits["query_cache_ttl_seconds"] == 60
    query = specification["components"]["schemas"]["QueryRequest"]
    assert query["required"] == ["dataset_id", "sql"]
    assert query["properties"]["max_rows"]["maximum"] == 1000
    assert query["properties"]["sql"]["x-max-utf8-bytes"] == 16384


def test_refresh_contract_distinguishes_queueing_replay_busy_and_owner_polling(specification):
    refresh = specification["paths"]["/api/v1/refresh"]["post"]
    assert {"200", "202", "409"} <= set(refresh["responses"])
    assert refresh["parameters"][0]["name"] == "Idempotency-Key"
    for status in ("200", "202"):
        assert "Location" in refresh["responses"][status]["headers"]
    poll = specification["paths"]["/api/v1/refresh/{job_id}"]["get"]
    assert "404" in poll["responses"] and "owner" in poll["description"]
    statuses = specification["components"]["schemas"]["RefreshJob"]["properties"]["status"]["enum"]
    assert statuses == ["queued", "running", "succeeded", "failed", "interrupted"]


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["viewer", "analyst", "admin"])
def test_packaged_openapi_is_served_to_each_persona_without_analytical_io(
    specification, authenticated_client, monkeypatch, role
):
    client, _ = authenticated_client(role)
    monkeypatch.setattr(
        api_services,
        "_bundle",
        lambda *_: pytest.fail("Static API documentation must not resolve model data."),
    )
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    assert response.json() == specification
    assert response["Cache-Control"] == "no-store"


@pytest.mark.django_db
def test_public_liveness_wire_shape_is_metadata_free_and_matches_the_spec(
    specification, api_context
):
    payload = Client().get("/api/v1/health/live").json()
    schema = specification["components"]["schemas"]["LiveResponse"]
    assert set(payload) == set(schema["required"]) == {"status"}
    assert payload["status"] == schema["properties"]["status"]["const"]


@pytest.mark.django_db
def test_query_example_and_scalar_wire_contract_match_the_actual_api(
    specification, authenticated_client, monkeypatch
):
    client, _ = authenticated_client("viewer")
    columns = [
        {"name": "period", "type": "DATE"},
        {"name": "offline_share", "type": "DECIMAL(24,12)"},
    ]
    monkeypatch.setattr(
        api_services,
        "_run",
        lambda *_: {"columns": columns, "rows": [["2026-09-01", "0.100000000000"]]},
    )
    example = specification["paths"]["/api/v1/query"]["post"]["requestBody"]["content"][
        "application/json"
    ]["example"]
    response = client.post("/api/v1/query", json.dumps(example), content_type="application/json")
    assert response.status_code == 200, response.content
    payload = response.json()
    schema = specification["components"]["schemas"]["QueryResponse"]
    assert set(payload) == set(schema["required"])
    assert payload["columns"] == columns
    assert payload["rows"] == [["2026-09-01", "0.100000000000"]]
    assert re.fullmatch(
        specification["components"]["schemas"]["DecimalString"]["pattern"], payload["rows"][0][1]
    )
    assert payload["cache_ttl_seconds"] == schema["properties"]["cache_ttl_seconds"]["const"]


@pytest.mark.django_db
def test_problem_details_wire_contract_matches_an_authentication_failure(
    specification, api_context
):
    response = Client().get("/api/v1/catalog")
    assert response.status_code == 401
    schema = specification["components"]["schemas"]["ProblemDetails"]
    assert set(response.json()) == set(schema["required"])
    assert response["Content-Type"].startswith("application/problem+json")
    assert response.json()["request_id"] == response["X-Request-ID"]
