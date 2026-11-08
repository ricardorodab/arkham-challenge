"""Execute every browser SQL starter through the real policy on isolated model data."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import duckdb
import pytest

from outage_explorer.analytics.model_contracts import DATASETS
from outage_explorer.data.domain import identifier
from outage_explorer.data.storage import Catalog
from outage_explorer.query.query_sandbox import runtime_status
from outage_explorer.query.sql_policy import validate_sql
from outage_explorer.service import api_services

MODULE = (
    Path(__file__).resolve().parents[1]
    / "src/outage_explorer/frontend/static/outage_explorer/sql_examples.js"
)
LEVELS = ("easy", "medium", "hard")
NODE_SCRIPT = """
import fs from 'node:fs';
const { sqlExamples } = await import(process.argv[1]);
const datasets = JSON.parse(fs.readFileSync(0, 'utf8'));
const examples = Object.fromEntries(datasets.map(dataset => [dataset.id, sqlExamples(dataset)]));
const sample = datasets.find(dataset => dataset.id === 'fleet_daily');
const first = sqlExamples(sample);
first[0].sql = 'changed locally';
const cases = {
  unknown: sqlExamples({id: 'auth_user', schema: []}),
  inheritedName: sqlExamples({id: 'constructor', schema: []}),
  malformed: sqlExamples({id: sample.id, schema: [null]}),
  missingColumn: sqlExamples({...sample, schema: sample.schema.filter(item => item[0] !== 'period')}),
  missingDataset: sqlExamples(null),
  freshCopy: sqlExamples(sample)[0].sql !== 'changed locally',
};
console.log(JSON.stringify({examples, cases}));
"""


@pytest.fixture(scope="module")
def starter_library():
    """Read the production ESM module with public schema metadata and no browser state."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the browser's SQL example module")
    completed = subprocess.run(
        [node, "--input-type=module", "-e", NODE_SCRIPT, MODULE.as_uri()],
        input=json.dumps([spec.catalog_definition() for spec in DATASETS.values()]),
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize("dataset_id", DATASETS)
@pytest.mark.parametrize("level", LEVELS)
def test_starter_is_supported_and_executable(starter_library, api_data_dir, dataset_id, level):
    """Compile one real starter and execute it over the synthetic pipeline's Parquet table."""
    examples = starter_library["examples"][dataset_id]
    assert [item["level"] for item in examples] == list(LEVELS)
    assert len({item["id"] for item in examples}) == 3
    selected = next(item for item in examples if item["level"] == level)
    assert selected["title"] and selected["description"]
    accepted = validate_sql(selected["sql"], dataset_id, max_rows=50)
    assert accepted.dataset_ids == (dataset_id,)
    with Catalog(api_data_dir) as catalog:
        model = catalog.current_model()
    assert model is not None
    with duckdb.connect(config={"threads": 1}) as connection:
        relation = connection.read_parquet(str(model.path / f"{dataset_id}.parquet"))
        relation.create_view(dataset_id)
        result = connection.execute(accepted.sql)
        assert result.description
        assert len(result.fetchall()) <= 51
        statements = connection.extract_statements(accepted.sql)
        assert len(statements) == 1 and statements[0].type == duckdb.StatementType.SELECT


def test_starters_require_compatible_catalog_metadata_and_return_detached_copies(starter_library):
    """Do not offer fixed-source templates for unknown or incomplete selected schemas."""
    cases = starter_library["cases"]
    assert cases == {
        "unknown": [],
        "inheritedName": [],
        "malformed": [],
        "missingColumn": [],
        "missingDataset": [],
        "freshCopy": True,
    }
    assert set(starter_library["examples"]) == set(DATASETS)


@pytest.mark.parametrize("dataset_id", DATASETS)
@pytest.mark.parametrize("level", LEVELS)
def test_starter_runs_through_authenticated_api_and_real_wasm(
    starter_library, authenticated_client, monkeypatch, dataset_id, level
):
    """Require the production runtime and execute the starter through CSRF/session HTTP."""
    assert runtime_status()["available"], "Starter acceptance requires the pinned WASM runtime."
    selected = next(
        item for item in starter_library["examples"][dataset_id] if item["level"] == level
    )
    executed = []
    run_query = api_services.run_query

    def observe_execution(*args, **kwargs):
        """Record returned runtime identity without replacing or weakening its execution."""
        data = run_query(*args, **kwargs)
        executed.append((data["sandbox"], data["engine"]))
        return data

    monkeypatch.setattr(api_services, "run_query", observe_execution)
    client, _ = authenticated_client("analyst")
    response = client.post(
        "/api/v1/query",
        json.dumps(
            {
                "dataset_id": dataset_id,
                "sql": selected["sql"],
                "max_rows": 50,
                "page_size": 50,
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    packet = response.json()
    assert packet["dataset_id"] == dataset_id and packet["columns"]
    assert not packet["cache_hit"]
    assert packet["returned_rows"] == len(packet["rows"]) <= 50
    assert packet["result_rows"] <= 50
    assert len(executed) == 1
    sandbox, engine = executed[0]
    assert sandbox == "wasm-buffer-v1" and engine.startswith("DuckDB-WASM ")


def test_generator_summary_preserves_composite_identity(starter_library):
    """Two facilities can reuse a generator ID without merging their summary groups."""
    selected = next(
        item for item in starter_library["examples"]["generator_daily"] if item["level"] == "hard"
    )
    accepted = validate_sql(selected["sql"], "generator_daily", max_rows=50)
    columns = ", ".join(
        f"{identifier(name)} {kind}" for name, kind in DATASETS["generator_daily"].columns
    )
    with duckdb.connect(config={"threads": 1}) as connection:
        connection.execute(f"CREATE TABLE generator_daily ({columns})")
        connection.execute(
            """
            INSERT INTO generator_daily
            (period, facility_id, generator_id, outage_mw)
            VALUES ('2026-09-01', '001', '01', 10),
                   ('2026-09-02', '001', '01', NULL),
                   ('2026-09-01', '002', '01', 30)
            """
        )
        result = connection.execute(accepted.sql).fetchall()
    assert [row[:4] for row in result] == [("002", "01", 1, 1), ("001", "01", 2, 1)]
