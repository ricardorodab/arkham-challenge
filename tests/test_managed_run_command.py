"""Exercise protected credential transport and actual deployment success boundaries offline."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "managed_run_command", ROOT / "deploy" / "invoke-managed-run-command.py"
)
command = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(command)


@pytest.fixture
def values():
    return command.inputs(
        {
            "AZURE_SUBSCRIPTION_ID": "11111111-2222-3333-4444-555555555555",
            "AZURE_RESOURCE_GROUP": "synthetic-group",
            "AZURE_VM_NAME": "synthetic-vm",
            "OUTAGE_PUBLIC_HOST": "synthetic.example",
            "OUTAGE_IMAGE": "ghcr.io/example/repository-vm@sha256:" + "b" * 64,
            "GITHUB_REPOSITORY": "example/repository",
            "GITHUB_SHA": "a" * 40,
            "GHCR_USERNAME": "synthetic-user",
            "GHCR_TOKEN": "synthetic-registry-token-never-use",
        }
    )


@pytest.mark.parametrize(
    "name,value",
    [
        ("OUTAGE_PUBLIC_HOST", "https://synthetic.example"),
        ("OUTAGE_PUBLIC_HOST", "synthetic.example;echo injected"),
        ("OUTAGE_IMAGE", "ghcr.io/example/repository:latest"),
        ("OUTAGE_IMAGE", "ghcr.io/example/repository@sha256:" + "b" * 64),
        ("OUTAGE_IMAGE", "ghcr.io/other/repository@sha256:" + "b" * 64),
        ("GITHUB_SHA", "main"),
        ("GITHUB_REPOSITORY", "example/repository/../../other"),
    ],
)
def test_deployment_inputs_reject_mutable_or_injected_public_identifiers(values, name, value):
    with pytest.raises(command.DeploymentError):
        command.inputs({**values, name: value})


@pytest.mark.parametrize("fail", [False, True])
def test_protected_json_body_is_private_removed_and_never_in_argv(monkeypatch, values, fail):
    observed = {}

    def request(arguments):
        assert values["GHCR_TOKEN"] not in str(arguments)
        path = Path(arguments[arguments.index("--body") + 1][1:])
        observed["path"] = path
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700
        assert json.loads(path.read_text())["properties"]["protectedParameters"] == [
            {"name": "GHCR_TOKEN", "value": values["GHCR_TOKEN"]}
        ]
        if fail:
            raise command.DeploymentError("Synthetic Azure failure.")
        return {}

    monkeypatch.setattr(command, "az_json", request)
    payload = {
        "properties": {
            "protectedParameters": [{"name": "GHCR_TOKEN", "value": values["GHCR_TOKEN"]}]
        }
    }
    if fail:
        with pytest.raises(command.DeploymentError):
            command.submit("https://management.azure.com/synthetic", payload)
    else:
        command.submit("https://management.azure.com/synthetic", payload)
    assert not observed["path"].exists()
    assert not observed["path"].parent.exists()


def test_azure_errors_do_not_echo_secret_adjacent_output(monkeypatch, values, capsys):
    token = values["GHCR_TOKEN"]
    monkeypatch.setenv("GHCR_TOKEN", token)

    def run(arguments, **options):
        assert token not in str(arguments)
        assert "GHCR_TOKEN" not in options["env"]
        assert options["capture_output"] is True
        return SimpleNamespace(returncode=1, stdout=token, stderr=token)

    monkeypatch.setattr(command.subprocess, "run", run)
    with pytest.raises(command.DeploymentError) as error:
        command.az_json(["--method", "get", "--url", "https://management.azure.com/synthetic"])
    assert token not in str(error.value)
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


def result(revision, **overrides):
    receipt = {"deployment": "healthy", "revision": revision, "https": True, "state": "preserved"}
    view = {"executionState": "Succeeded", "exitCode": 0, "output": json.dumps(receipt)}
    view.update(overrides)
    return {"properties": {"provisioningState": "Succeeded", "instanceView": view}}


@pytest.mark.parametrize(
    "overrides",
    [
        {"exitCode": None},
        {"exitCode": True},
        {"exitCode": 75},
        {"executionState": "Failed"},
        {"executionState": "TimedOut"},
        {"output": '{"deployment":"deferred"}'},
        {"output": "Unstructured success text"},
    ],
)
def test_provisioning_success_cannot_mask_guest_failure_or_missing_receipt(values, overrides):
    with pytest.raises(command.DeploymentError):
        command.completed(result(values["GITHUB_SHA"], **overrides), values["GITHUB_SHA"])


def test_completion_requires_this_revision_and_a_terminal_guest_result(values):
    revision = values["GITHUB_SHA"]
    assert not command.completed({"properties": {"provisioningState": "Succeeded"}}, revision)
    assert not command.completed(result(revision, executionState="Running"), revision)
    assert command.completed(result(revision), revision)
    with pytest.raises(command.DeploymentError):
        command.completed(result("c" * 40), revision)


def test_deployment_binds_protected_token_exact_source_digest_and_public_verification(
    monkeypatch, values
):
    observed = {}
    responses = iter(
        [
            {"location": "synthetic-region"},
            result(values["GITHUB_SHA"], executionState="Running"),
            result(values["GITHUB_SHA"]),
        ]
    )
    urls = []

    def request(arguments, **_options):
        urls.append(arguments[arguments.index("--url") + 1])
        if arguments[arguments.index("--method") + 1] == "delete":
            observed["deleted"] = True
            return {}
        return next(responses)

    monkeypatch.setattr(command, "az_json", request)
    monkeypatch.setattr(command, "submit", lambda url, body: observed.update(url=url, body=body))
    monkeypatch.setattr(command.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        command, "verify_public_health", lambda *args: observed.update(public_health=args)
    )
    receipt = command.deploy(values)
    assert receipt == {"deployment": "healthy", "revision": values["GITHUB_SHA"], "https": True}
    properties = observed["body"]["properties"]
    assert properties["protectedParameters"] == [
        {"name": "GHCR_TOKEN", "value": values["GHCR_TOKEN"]}
    ]
    assert properties["parameters"] == [{"name": "GHCR_USERNAME", "value": values["GHCR_USERNAME"]}]
    assert properties["treatFailureAsDeploymentFailure"] is True
    script = properties["source"]["script"]
    assert script.startswith("/bin/bash <<'ARKHAM_MANAGED_RUN'\n")
    assert values["GHCR_TOKEN"] not in script
    assert values["GITHUB_SHA"] in script and values["OUTAGE_IMAGE"] in script
    assert "https://codeload.github.com/" in script
    assert "archive.extractall" not in script
    assert all(name in script for name in command.DEPLOY_FILES)
    assert all("$expand=instanceView" in url for url in urls[1:3])
    assert observed["deleted"] is True
    assert observed["public_health"] == (values["OUTAGE_PUBLIC_HOST"], values["GITHUB_SHA"])


@pytest.mark.parametrize("state,deleted", [("Failed", True), ("Running", False)])
def test_failed_terminal_commands_are_cleaned_but_running_timeouts_are_preserved(
    monkeypatch, values, state, deleted
):
    observed = {"deleted": False}

    def request(arguments, **_options):
        method = arguments[arguments.index("--method") + 1]
        url = arguments[arguments.index("--url") + 1]
        if method == "delete":
            observed["deleted"] = True
            return {}
        if "/runCommands/" not in url:
            return {"location": "synthetic-region"}
        return result(values["GITHUB_SHA"], executionState=state)

    monkeypatch.setattr(command, "az_json", request)
    monkeypatch.setattr(command, "submit", lambda *_: None)
    monkeypatch.setattr(command.time, "sleep", lambda _: None)
    ticks = iter([0, 0, command.POLL_TIMEOUT_SECONDS + 1])
    monkeypatch.setattr(command.time, "monotonic", lambda: next(ticks))
    with pytest.raises(command.DeploymentError):
        command.deploy(values)
    assert observed["deleted"] is deleted


def test_generated_guest_shell_and_embedded_python_are_syntactically_valid(values):
    script = command.guest_script(values)
    program = script.split("python3 - <<'ARKHAM_SOURCE'\n", 1)[1].split("ARKHAM_SOURCE\n", 1)[0]
    compile(program, "managed-run-command-source", "exec")
    bash = shutil.which("bash")
    if os.name == "nt":
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(candidate) if candidate.is_file() else None
    if bash:
        result = subprocess.run(
            [bash, "-n"], input=script, text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("revision,body", [("c" * 40, b'{"status":"ok"}'), ("a" * 40, b"{}")])
def test_public_https_verification_rejects_stale_revision_or_wrong_body(
    monkeypatch, values, revision, body
):
    class Response:
        status = 200
        headers = {"X-Outage-Revision": revision}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def geturl(self):
            return "https://synthetic.example/api/v1/health/live"

        def read(self, _):
            return body

    monkeypatch.setattr(
        command.urllib.request,
        "build_opener",
        lambda *_: SimpleNamespace(open=lambda *_args, **_kwargs: Response()),
    )
    with pytest.raises(command.DeploymentError):
        command.verify_public_health(values["OUTAGE_PUBLIC_HOST"], values["GITHUB_SHA"])
