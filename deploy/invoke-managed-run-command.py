"""Deploy a tested public image digest using Azure OIDC and Managed Run Command.

No registry credential, SSH access, GitHub artifact containing secrets, or static Azure
credential is needed. A private temporary REST body carries the guest script. The public
source archive supplies only deployment files; runtime secrets and data stay on the VM.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

API_VERSION = "2025-04-01"
SCRIPT_TIMEOUT_SECONDS = 1200
POLL_TIMEOUT_SECONDS = 1500
DEPLOY_FILES = (
    "bootstrap-vm.sh",
    "release.sh",
    "compose.production.yaml",
    "Caddyfile",
    "app-control.py",
    "verify-state.py",
    "key-vault-secrets.py",
    "backup.py",
    "restore.py",
    "backup-run.sh",
    "restore-drill.sh",
)


class DeploymentError(RuntimeError):
    """Contain only deliberate, credential-free operational messages."""


def inputs(environment):
    """Validate public identifiers before constructing URLs, paths and the guest script."""
    names = (
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_RESOURCE_GROUP",
        "AZURE_VM_NAME",
        "OUTAGE_PUBLIC_HOST",
        "OUTAGE_IMAGE",
        "GITHUB_REPOSITORY",
        "GITHUB_SHA",
    )
    values = {}
    for name in names:
        value = environment.get(name, "")
        if not value or value != value.strip():
            raise DeploymentError(f"Missing or invalid deployment input: {name}.")
        values[name] = value
    patterns = {
        "AZURE_SUBSCRIPTION_ID": r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
        "AZURE_RESOURCE_GROUP": r"[A-Za-z0-9_.()-]{1,90}",
        "AZURE_VM_NAME": r"[A-Za-z0-9_.-]{1,64}",
        "GITHUB_REPOSITORY": r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*",
        "GITHUB_SHA": r"[0-9a-f]{40}",
        "OUTAGE_PUBLIC_HOST": (
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
        ),
    }
    for name, pattern in patterns.items():
        if not re.fullmatch(pattern, values[name]):
            raise DeploymentError(f"Invalid deployment input: {name}.")
    image_prefix = "ghcr.io/" + values["GITHUB_REPOSITORY"].lower() + "-vm@sha256:"
    if not re.fullmatch(re.escape(image_prefix) + r"[0-9a-f]{64}", values["OUTAGE_IMAGE"]):
        raise DeploymentError("The image must be this repository's immutable GHCR digest.")
    return values


def guest_script(values):
    """Fetch only the exact public commit's required regular deployment files."""
    repository = values["GITHUB_REPOSITORY"]
    revision = values["GITHUB_SHA"]
    release = "/opt/arkham/releases/" + revision
    # Manual member reads avoid extracting archive paths, symlinks or unrelated source.
    download = f"""import io, os, tarfile, urllib.request
from pathlib import Path

revision = {revision!r}
repository = {repository!r}
release = Path({release!r})
for directory in (Path('/opt/arkham'), Path('/opt/arkham/releases'), release):
    if directory.is_symlink():
        raise SystemExit('Release directories cannot be symlinks.')
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.stat().st_uid != 0:
        raise SystemExit('Release directories must belong to root.')
    os.chmod(directory, 0o700)
url = 'https://codeload.github.com/' + repository + '/tar.gz/' + revision
with urllib.request.urlopen(url, timeout=120) as response:
    content = response.read(64 * 1024 * 1024 + 1)
if len(content) > 64 * 1024 * 1024:
    raise SystemExit('Source archive is larger than the deployment limit.')
deploy = release / 'deploy'
if deploy.is_symlink():
    raise SystemExit('Deployment files cannot be symlinks.')
deploy.mkdir(mode=0o700, exist_ok=True)
prefix = repository.split('/')[1] + '-' + revision + '/deploy/'
with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as archive:
    for name in {DEPLOY_FILES!r}:
        member = archive.getmember(prefix + name)
        if not member.isfile() or member.size > 2 * 1024 * 1024:
            raise SystemExit('Invalid deployment archive member.')
        data = archive.extractfile(member).read()
        path = deploy / name
        if path.is_symlink():
            raise SystemExit('Deployment files cannot be symlinks.')
        if path.exists():
            if not path.is_file() or path.read_bytes() != data:
                raise SystemExit('An existing immutable release differs from its source.')
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
        os.chmod(path, 0o644)
"""
    return (
        "/bin/bash <<'ARKHAM_MANAGED_RUN'\nset -Eeuo pipefail\nset +x\numask 077\n"
        "[[ ${EUID} -eq 0 ]] || exit 1\n"
        "python3 - <<'ARKHAM_SOURCE'\n" + download + "ARKHAM_SOURCE\n"
        f"bash {shlex.quote(release + '/deploy/bootstrap-vm.sh')} "
        f"{shlex.quote(values['OUTAGE_PUBLIC_HOST'])}\n"
        f"bash {shlex.quote(release + '/deploy/release.sh')} "
        f"{shlex.quote(revision)} {shlex.quote(values['OUTAGE_IMAGE'])}\n"
        "ARKHAM_MANAGED_RUN\n"
    )


def az_json(arguments, *, allow_empty=False):
    """Capture Azure CLI responses and errors; never echo secret-adjacent Azure output."""
    environment = dict(os.environ)
    environment["AZURE_CORE_ONLY_SHOW_ERRORS"] = "true"
    try:
        result = subprocess.run(
            ["az", "rest", *arguments, "--output", "json", "--only-show-errors"],
            capture_output=True,
            text=True,
            timeout=90,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DeploymentError("Azure management request could not complete.") from None
    if result.returncode:
        raise DeploymentError("Azure management request failed; inspect the Run Command in Azure.")
    if allow_empty and not result.stdout.strip():
        return {}
    try:
        document = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise DeploymentError("Azure returned an invalid management response.") from None
    if not isinstance(document, dict):
        raise DeploymentError("Azure returned an unexpected management response.")
    return document


def submit(command_url, payload):
    """Transport the management request through a mode-0600 file and remove it immediately."""
    with tempfile.TemporaryDirectory(prefix="arkham-run-command-") as directory:
        path = Path(directory) / "request.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        az_json(["--method", "put", "--url", command_url, "--body", "@" + str(path)])


def completed(document, revision):
    """Require guest execution success, zero exit code and the release's success receipt."""
    properties = document.get("properties", {})
    view = properties.get("instanceView", {})
    state = str(view.get("executionState", "")).lower()
    if state in {"failed", "canceled", "cancelled", "timedout"} or str(
        properties.get("provisioningState", "")
    ).lower() in {"failed", "canceled", "cancelled"}:
        raise DeploymentError("The VM deployment command failed; inspect its private Azure result.")
    if state != "succeeded":
        return False
    if type(view.get("exitCode")) is not int or view["exitCode"] != 0:
        raise DeploymentError("The VM command did not report a successful zero exit code.")
    for line in reversed(str(view.get("output", "")).splitlines()):
        try:
            receipt = json.loads(line)
        except ValueError:
            continue
        if receipt == {
            "deployment": "healthy",
            "revision": revision,
            "https": True,
            "state": "preserved",
        }:
            return True
    raise DeploymentError("The VM command did not confirm this release's healthy deployment.")


def verify_public_health(host, revision):
    """Independently validate public TLS, response shape and exact deployed revision."""
    url = "https://" + host + "/api/v1/health/live"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with opener.open(request, timeout=20) as response:
            body = response.read(1025)
            valid = (
                response.status == 200
                and response.geturl() == url
                and response.headers.get("X-Outage-Revision") == revision
                and len(body) <= 1024
                and json.loads(body) == {"status": "ok"}
            )
    except (OSError, urllib.error.URLError, ValueError):
        raise DeploymentError("Public HTTPS deployment verification failed.") from None
    if not valid:
        raise DeploymentError("Public HTTPS did not report the expected deployment revision.")


def deploy(values):
    """Submit one unique managed command, wait for its result, then check the public origin."""
    resource = (
        "https://management.azure.com/subscriptions/"
        + urllib.parse.quote(values["AZURE_SUBSCRIPTION_ID"], safe="")
        + "/resourceGroups/"
        + urllib.parse.quote(values["AZURE_RESOURCE_GROUP"], safe="")
        + "/providers/Microsoft.Compute/virtualMachines/"
        + urllib.parse.quote(values["AZURE_VM_NAME"], safe="")
    )
    vm = az_json(["--method", "get", "--url", resource + "?api-version=" + API_VERSION])
    location = vm.get("location")
    if not isinstance(location, str) or not location:
        raise DeploymentError("Azure did not return the target VM location.")
    name = "arkham-deploy-" + values["GITHUB_SHA"][:12] + "-" + uuid.uuid4().hex[:8]
    command_url = resource + "/runCommands/" + name + "?api-version=" + API_VERSION
    submit(
        command_url,
        {
            "location": location,
            "properties": {
                "source": {"script": guest_script(values)},
                "asyncExecution": True,
                "treatFailureAsDeploymentFailure": True,
                "timeoutInSeconds": SCRIPT_TIMEOUT_SECONDS,
            },
        },
    )
    terminal = False
    try:
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            document = az_json(["--method", "get", "--url", command_url + "&$expand=instanceView"])
            state = str(
                document.get("properties", {}).get("instanceView", {}).get("executionState", "")
            ).lower()
            terminal = state in {"succeeded", "failed", "canceled", "cancelled", "timedout"}
            if completed(document, values["GITHUB_SHA"]):
                break
            time.sleep(5)
        else:
            raise DeploymentError("Timed out waiting for the VM deployment result; inspect Azure.")
    finally:
        # Azure limits managed commands per VM. Never delete an unknown/running command:
        # deleting it would terminate the guest script, including any in-progress upgrade.
        if terminal:
            try:
                az_json(["--method", "delete", "--url", command_url], allow_empty=True)
            except DeploymentError:
                print(
                    json.dumps({"warning": "terminal_run_command_cleanup_failed", "name": name}),
                    file=sys.stderr,
                )
    verify_public_health(values["OUTAGE_PUBLIC_HOST"], values["GITHUB_SHA"])
    return {"deployment": "healthy", "revision": values["GITHUB_SHA"], "https": True}


def main():
    """Print only a credential-free deployment receipt or a deliberate safe failure."""
    try:
        result = deploy(inputs(os.environ))
    except DeploymentError as error:
        print(json.dumps({"deployment": "failed", "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
