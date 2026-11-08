"""Keep the packaged OpenAPI contract aligned with the actual HTTP capabilities."""

from __future__ import annotations

import ast
import inspect
import json
import re
from datetime import datetime
from importlib.resources import files

import pytest
from django.test import Client

from outage_explorer.analytics.model_contracts import DATASETS
from outage_explorer.query.sql_policy import MAX_ROWS, MAX_SQL_BYTES
from outage_explorer.service import api_services, urls, views
from outage_explorer.service.models import RefreshJob


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
    assert (
        len(operations) == len(set(operations)) == sum(len(methods) for methods in actual.values())
    )


def test_all_references_resolve_and_every_operation_has_typed_success_and_safe_errors(
    specification,
):
    for node in walk(specification):
        if isinstance(node, dict) and "$ref" in node:
            assert isinstance(resolve(specification, node["$ref"]), dict)
    for item in specification["paths"].values():
        for operation in item.values():
            successes = [
                value for code, value in operation["responses"].items() if code.startswith("2")
            ]
            assert successes
            assert all(value["content"]["application/json"]["schema"] for value in successes)
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
        for method in {"post", "put", "patch", "delete"} & item.keys():
            required = set(item[method]["security"][0])
            assert {"CSRFCookie", "CSRFHeader"} <= required
            assert ("SessionCookie" in required) is (path != "/api/v1/auth/login")
    for path in (
        "/api/v1/refresh",
        "/api/v1/refresh/{job_id}",
        "/api/v1/health/ready",
        "/api/v1/users",
        "/api/v1/users/{user_id}",
        "/api/v1/users/{user_id}/password",
    ):
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
    assert "default" not in query["properties"]["page_size"]
    assert "min(100, max_rows)" in query["properties"]["page_size"]["description"]
    assert limits["compiler_timeout_seconds"] == 2 and limits["queue_wait_seconds"] == 5
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


def test_refresh_progress_contract_is_closed_bounded_and_has_no_overall_denominator(specification):
    schemas = specification["components"]["schemas"]
    job = schemas["RefreshJob"]
    assert "progress" in job["required"]
    assert job["properties"]["progress"] == {"$ref": "#/components/schemas/RefreshProgress"}
    empty, snapshot = schemas["RefreshProgress"]["oneOf"]
    assert empty == {"type": "object", "properties": {}, "additionalProperties": False}
    assert snapshot["additionalProperties"] is False
    assert set(snapshot["required"]) == {
        "stage",
        "updated_at",
        "rows_received",
        "pages_fetched",
    }
    properties = snapshot["properties"]
    assert set(properties) == {
        "stage",
        "updated_at",
        "rows_received",
        "pages_fetched",
        "route",
        "routes",
        "completed",
        "total",
    }
    assert set(properties["stage"]["enum"]) == {
        "preparing",
        "metadata",
        "extracting",
        "validating_source",
        "model_sources",
        "model_facts",
        "model_fleet",
        "model_reconciliation",
        "model_quality",
        "validating_model",
        "exporting_model",
        "verifying_model",
        "publishing",
        "completed",
    }
    assert properties["updated_at"] == {"type": "string", "format": "date-time"}
    counts = schemas["RefreshProgressCounts"]
    assert counts["additionalProperties"] is False
    assert (
        set(counts["properties"])
        == set(counts["required"])
        == {
            "rows_received",
            "pages_fetched",
        }
    )
    for name in ("rows_received", "pages_fetched"):
        assert (
            properties[name]
            == counts["properties"][name]
            == {
                "type": "integer",
                "minimum": 0,
                "maximum": (1 << 53) - 1,
            }
        )
    assert set(properties["route"]["enum"]) == {"national", "facility", "generator"}
    routes = properties["routes"]
    assert routes["additionalProperties"] is False
    assert set(routes["properties"]) == {"national", "facility", "generator"}
    assert all(
        value == {"$ref": "#/components/schemas/RefreshProgressCounts"}
        for value in routes["properties"].values()
    )
    assert properties["total"] == {"type": "integer", "const": len(DATASETS)}
    assert properties["completed"] == {"type": "integer", "minimum": 0, "maximum": 10}
    assert len(DATASETS) == 10
    assert snapshot["dependentRequired"] == {"completed": ["total"], "total": ["completed"]}
    assert snapshot["if"]["properties"]["stage"]["const"] == "exporting_model"
    assert {tuple(item["required"]) for item in snapshot["else"]["not"]["anyOf"]} == {
        ("completed",),
        ("total",),
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    "progress",
    [
        {},
        {
            "stage": "extracting",
            "updated_at": "2026-11-08T04:15:00+00:00",
            "route": "facility",
            "rows_received": 12,
            "pages_fetched": 2,
            "routes": {"facility": {"rows_received": 12, "pages_fetched": 2}},
        },
        {
            "stage": "exporting_model",
            "updated_at": "2026-11-08T04:15:00+00:00",
            "completed": 4,
            "total": 10,
        },
    ],
    ids=["legacy", "receiving-pages", "bounded-export"],
)
def test_refresh_job_wire_progress_matches_its_required_schema(
    specification, authenticated_client, progress
):
    client, owner = authenticated_client("admin")
    job = RefreshJob.objects.create(owner=owner, mode="full", status="running", progress=progress)
    response = client.get(f"/api/v1/refresh/{job.id}")
    assert response.status_code == 200
    payload = response.json()
    schemas = specification["components"]["schemas"]
    assert set(payload) == set(schemas["RefreshJob"]["required"])
    assert schemas["RefreshJob"]["additionalProperties"] is False
    actual = payload["progress"]
    if not progress:
        assert actual == {}
        return
    snapshot = schemas["RefreshProgress"]["oneOf"][1]
    assert set(snapshot["required"]) <= set(actual) <= set(snapshot["properties"])
    assert actual["stage"] in snapshot["properties"]["stage"]["enum"]
    assert datetime.fromisoformat(actual["updated_at"]).utcoffset() is not None
    for name in ("rows_received", "pages_fetched"):
        assert type(actual[name]) is int
        assert 0 <= actual[name] <= snapshot["properties"][name]["maximum"]
    if actual["stage"] == "exporting_model":
        assert actual["total"] == snapshot["properties"]["total"]["const"]
        assert 0 <= actual["completed"] <= actual["total"]
    else:
        assert "completed" not in actual and "total" not in actual
        assert actual["route"] in snapshot["properties"]["route"]["enum"]
        assert set(actual["routes"]) <= set(snapshot["properties"]["routes"]["properties"])
        assert set(actual["routes"]["facility"]) == set(
            schemas["RefreshProgressCounts"]["required"]
        )


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
def test_session_wire_scope_is_opaque_and_matches_the_required_schema(
    specification, authenticated_client, monkeypatch
):
    client, _ = authenticated_client("viewer")
    monkeypatch.setattr(
        api_services, "_bundle", lambda *_: pytest.fail("Session identity must not resolve data")
    )
    response = client.get("/api/v1/auth/session")
    assert response.status_code == 200
    payload = response.json()
    schema = specification["components"]["schemas"]["SessionResponse"]
    assert set(payload) == set(schema["required"]) == {"user", "session_scope"}
    pattern = schema["properties"]["session_scope"]["pattern"]
    assert re.fullmatch(pattern, payload["session_scope"])
    assert client.session.session_key not in response.content.decode()
    assert all(re.fullmatch(pattern, example["session_scope"]) for example in schema["examples"])


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


def test_available_runtime_status_matches_its_strict_public_schema(specification):
    from outage_explorer.query.query_sandbox import runtime_status

    status = runtime_status()
    assert status["available"], "Contract acceptance requires the pinned query runtime."
    schema = specification["components"]["schemas"]["RuntimeStatus"]
    assert schema["additionalProperties"] is False
    assert set(status) == set(schema["required"]) == set(schema["properties"])
    for name, value in status.items():
        field = schema["properties"][name]
        if "const" in field:
            assert type(value) is type(field["const"]) and value == field["const"]
        elif "enum" in field:
            assert value in field["enum"]
        else:
            assert field["type"] == "string" and isinstance(value, str)


@pytest.mark.django_db
def test_account_directory_wire_shape_matches_the_private_admin_contract(
    specification, authenticated_client, monkeypatch
):
    """Keep account metadata independent of analytical data and credential responses."""
    client, _ = authenticated_client("admin")
    monkeypatch.setattr(
        api_services,
        "_bundle",
        lambda *_: pytest.fail("Account management must not resolve analytical data."),
    )
    response = client.get("/api/v1/users", {"page_size": 1})
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    payload = response.json()
    schemas = specification["components"]["schemas"]
    assert set(payload) == set(schemas["ManagedUserList"]["required"])
    assert len(payload["users"]) == 1
    account = payload["users"][0]
    assert set(account) == set(schemas["ManagedUser"]["required"])
    assert re.fullmatch(
        schemas["ManagedUser"]["properties"]["version"]["pattern"], account["version"]
    )
    assert {"password", "generated_password", "session_version", "session_key"}.isdisjoint(account)


@pytest.mark.django_db
def test_generated_account_credentials_match_the_one_time_response_schema(
    specification, authenticated_client
):
    """Creation returns the credential once while later directory pages omit it."""
    client, _ = authenticated_client("admin")
    response = client.post(
        "/api/v1/users",
        json.dumps({"username": "contract-created-user", "role": "viewer"}),
        content_type="application/json",
    )
    assert response.status_code == 201
    assert response["Cache-Control"] == "no-store"
    payload = response.json()
    schemas = specification["components"]["schemas"]
    schema = schemas["UserCreateResponse"]
    assert set(payload) == set(schema["required"])
    assert payload["reauthenticate"] is schema["properties"]["reauthenticate"]["const"]
    password = schema["properties"]["generated_password"]
    assert re.fullmatch(password["pattern"], payload["generated_password"])
    assert len(payload["generated_password"]) == password["minLength"] == password["maxLength"]
    directory = client.get("/api/v1/users", {"search": payload["user"]["username"]})
    assert directory.status_code == 200
    assert payload["generated_password"] not in directory.content.decode()
    assert directory.json()["users"] == [payload["user"]]
