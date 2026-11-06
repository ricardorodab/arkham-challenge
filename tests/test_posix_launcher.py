"""Verify POSIX kernel limits through the real exec launcher without a Node worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from outage_explorer import query_sandbox as sandbox


@pytest.mark.skipif(os.name == "nt", reason="POSIX resource-limit boundary")
@pytest.mark.parametrize(
    "launcher_options,data_limit_mib",
    [((), 512), (("--engine",), 1024 if sys.platform == "linux" else 512)],
    ids=["compiler-default", "engine"],
)
def test_posix_launcher_keeps_kernel_limits_and_denies_large_allocation(
    tmp_path, launcher_options, data_limit_mib
):
    """Observe limits after exec and require a real kernel-backed allocation denial."""
    executable = str(Path(sys.executable).resolve())
    probe = (
        "import json,resource,sys\n"
        "names=('RLIMIT_DATA','RLIMIT_CPU','RLIMIT_CORE','RLIMIT_FSIZE','RLIMIT_NOFILE')\n"
        "print(json.dumps({name:resource.getrlimit(getattr(resource,name)) for name in names}),"
        "flush=True)\n"
        f"try: bytearray({data_limit_mib + 188}*1024*1024)\n"
        "except MemoryError: sys.exit(77)\n"
        "sys.exit(78)\n"
    )
    completed = subprocess.run(
        [
            executable,
            "-I",
            "-S",
            "-B",
            str(sandbox.ASSETS / "posix_launcher.py"),
            *launcher_options,
            executable,
            "-I",
            "-S",
            "-B",
            "-c",
            probe,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=sandbox._environment(),
        cwd=tmp_path,
        start_new_session=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 77
    assert json.loads(completed.stdout) == {
        "RLIMIT_DATA": [data_limit_mib * 1024 * 1024, data_limit_mib * 1024 * 1024],
        "RLIMIT_CPU": [6, 6],
        "RLIMIT_CORE": [0, 0],
        "RLIMIT_FSIZE": [0, 0],
        "RLIMIT_NOFILE": [64, 64],
    }
