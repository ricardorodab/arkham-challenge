"""Kill actual child processes at the two publication boundaries, without sleeps."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from outage_explorer.pipeline import IngestConfig, ingest
from outage_explorer.storage import Catalog
from tests.helpers import FakeClient


@pytest.mark.parametrize("boundary,exit_code", [("renamed", 77), ("committed", 78)])
def test_actual_process_death_preserves_publication_and_releases_lock(
    tmp_path, boundary, exit_code
):
    baseline = ingest(IngestConfig(tmp_path, "full"), FakeClient())
    script = """
import os, sys
from pathlib import Path
from outage_explorer import pipeline
from outage_explorer.storage import Catalog
from tests.helpers import FakeClient
boundary=sys.argv[2]
if boundary == 'renamed':
    original=pipeline.os.replace
    def stop_after_rename(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(77)
    pipeline.os.replace=stop_after_rename
else:
    original=Catalog.publish
    def stop_after_commit(self, *args, **kwargs):
        original(self, *args, **kwargs)
        os._exit(78)
    Catalog.publish=stop_after_commit
pipeline.ingest(pipeline.IngestConfig(Path(sys.argv[1]), 'full'), FakeClient())
"""
    project = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.pop("EIA_API_KEY", None)
    environment["PYTHONPATH"] = os.pathsep.join((str(project / "src"), str(project)))
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), boundary],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert child.returncode == exit_code, child.stderr
    with Catalog(tmp_path) as catalog:
        after_crash = catalog.current()
        assert catalog.verify()["verified"]
        if boundary == "renamed":
            assert after_crash.id == baseline["snapshot_id"]
            assert catalog.history()[0]["status"] == "running"
        else:
            assert after_crash.id != baseline["snapshot_id"]
            assert catalog.history()[0]["status"] == "succeeded"
    # Real OS lock ownership vanished with the child. Recovery must preserve
    # committed success and mark only an uncommitted run as interrupted.
    ingest(IngestConfig(tmp_path, "recent"), FakeClient())
    with Catalog(tmp_path) as catalog:
        assert catalog.verify()["verified"]
        states = [row["status"] for row in catalog.history()]
        assert states.count("interrupted") == (1 if boundary == "renamed" else 0)
        assert states.count("succeeded") == (2 if boundary == "renamed" else 3)
