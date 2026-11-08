"""Verify Docker build-context privacy with synthetic canaries and an existing image.

Copy only explicit production manifests and Git-tracked application sources into an owned
scratch context. Never copy ignored operator data or credentials. A disposable COPY-only
image proves the current .dockerignore excludes the synthetic private paths while preserving
required source bytes. Host requirements are Python's standard library, Git and Docker CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABEL = "org.outage-explorer.context-privacy"
MANIFESTS = (
    ".dockerignore",
    "Dockerfile",
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "package-lock.json",
)
BLOCKED = (
    ".env",
    "key.txt",
    "data/canary.txt",
    ".service/local-config.json",
    "work/canary.txt",
    "docs/AI/canary.md",
    "TODO.md",
    "PLAN.md",
    "src/outage_explorer/private/key.py",
    "src/outage_explorer/operator.pem",
    "src/outage_explorer/operator-notes.txt",
    ".venv/canary.txt",
    "node_modules/canary.txt",
)
PROBE = r"""
import hashlib,json,sys
from pathlib import Path
root=Path('/context')
marker=sys.argv[1].encode()
expected=json.loads(sys.argv[2])
blocked=json.loads(sys.argv[3])
if any((root/name).exists() for name in blocked):
    raise SystemExit(1)
for name,digest in expected.items():
    path=root/name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
        raise SystemExit(2)
count=0
for path in root.rglob('*'):
    if path.is_file():
        if marker in path.read_bytes():
            raise SystemExit(3)
        count+=1
print(json.dumps({'files_checked':count,'required_files':len(expected),
                  'blocked_canaries':len(blocked)}))
"""


class AuditFailure(Exception):
    """Stop the audit without exposing captured build output or source contents."""


def require(condition: bool) -> None:
    """Raise a static failure when an ownership, path or audit invariant is false."""
    if not condition:
        raise AuditFailure


class ContextAudit:
    """Own one uniquely labelled audit image and its bounded scan container."""

    def __init__(self, docker: str, image: str) -> None:
        """Resolve resource names without creating images, containers or host files."""
        self.docker = docker
        self.base = image
        self.token = uuid.uuid4().hex
        self.image = "arkham-context-audit:" + self.token
        self.container = "arkham-context-audit-" + self.token
        self.stage = "prerequisites"
        self.cleanup_ok = True

    def command(self, arguments: list[str], *, timeout: int = 30, check: bool = True):
        """Capture a Docker invocation and discard diagnostics on failure.

        Timeouts are deadlock watchdogs, not performance assertions. No shell is used and
        captured output is never included in the public failure summary.
        """
        result = subprocess.run(
            [self.docker, *arguments], capture_output=True, text=True, timeout=timeout, check=False
        )
        if check:
            require(result.returncode == 0)
        return result

    def populate(self, context: Path) -> dict[str, str]:
        """Copy current tracked public sources and create synthetic blocked-path canaries.

        Reject symlinks and path escape rather than following a tracked entry into private
        state. The source list comes from Git, not recursive enumeration of the checkout.
        Return hashes for every required copied file, including the exact ignore policy.
        """
        git = shutil.which("git")
        require(git is not None)
        result = subprocess.run(
            [git, "ls-files", "-z", "--", "src/"],
            cwd=ROOT,
            capture_output=True,
            timeout=20,
            check=False,
        )
        require(result.returncode == 0)
        sources = result.stdout.decode("utf-8").split("\0")
        names = sorted(set(MANIFESTS) | {name for name in sources if name})
        require(any(name.endswith(".wasm") for name in names))
        expected = {}
        for name in names:
            relative = Path(name)
            require(not relative.is_absolute() and ".." not in relative.parts)
            source = ROOT / relative
            require(source.is_file())
            require(
                all(not part.is_symlink() for part in (source, *source.parents) if part != ROOT)
            )
            require(source.resolve().is_relative_to(ROOT.resolve()))
            destination = context / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = source.read_bytes()
            destination.write_bytes(payload)
            expected[name] = hashlib.sha256(payload).hexdigest()
        marker = "ARKHAM_PRIVATE_CONTEXT_CANARY_" + self.token
        for name in BLOCKED:
            destination = context / name
            require(not destination.exists())
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(marker, encoding="utf-8")
        (context / "Dockerfile.audit").write_text(
            f"FROM {self.base}\nCOPY . /context\n", encoding="utf-8"
        )
        return expected

    def run(self) -> dict[str, int]:
        """Build without pulling and scan a disposable context image without networking."""
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", self.base) is not None)
        self.command(["image", "inspect", self.base])
        require(self.command(["image", "inspect", self.image], check=False).returncode != 0)
        require(self.command(["container", "inspect", self.container], check=False).returncode != 0)
        with tempfile.TemporaryDirectory(prefix="arkham-context-") as temporary:
            context = Path(temporary).resolve()
            require(context.parent == Path(tempfile.gettempdir()).resolve())
            require(context.name.startswith("arkham-context-"))
            self.stage = "synthetic_context"
            expected = self.populate(context)
            self.stage = "copy_only_build"
            self.command(
                [
                    "build",
                    "--pull=false",
                    "--network=none",
                    "--label",
                    f"{LABEL}={self.token}",
                    "--tag",
                    self.image,
                    "--file",
                    str(context / "Dockerfile.audit"),
                    str(context),
                ],
                timeout=180,
            )
            self.stage = "private_path_and_content_scan"
            result = self.command(
                [
                    "run",
                    "--rm",
                    "--name",
                    self.container,
                    "--label",
                    f"{LABEL}={self.token}",
                    "--network=none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges:true",
                    self.image,
                    "python",
                    "-B",
                    "-c",
                    PROBE,
                    "ARKHAM_PRIVATE_CONTEXT_CANARY_" + self.token,
                    json.dumps(expected),
                    json.dumps(BLOCKED),
                ]
            )
            summary = json.loads(result.stdout)
            require(set(summary) == {"files_checked", "required_files", "blocked_canaries"})
            require(all(isinstance(value, int) for value in summary.values()))
            require(summary["required_files"] == len(expected))
            require(summary["blocked_canaries"] == len(BLOCKED))
            return summary

    def cleanup(self) -> None:
        """Remove only exact-name resources carrying this audit's unique ownership label.

        An absent --rm container is normal. Refuse removal on any label mismatch and never
        prune images, containers, volumes or build cache belonging to another task.
        """
        for kind, name in (("container", self.container), ("image", self.image)):
            try:
                listing = (
                    ["container", "ls", "--all", "--quiet", "--filter", f"name=^/{name}$"]
                    if kind == "container"
                    else ["image", "ls", "--quiet", "--filter", f"reference={name}"]
                )
                if not self.command(listing).stdout.strip():
                    continue
                metadata = json.loads(self.command([kind, "inspect", name]).stdout)[0]
                labels = metadata.get("Config", {}).get("Labels") or {}
                require(labels.get(LABEL) == self.token)
                arguments = (
                    ["rm", "--force", name] if kind == "container" else ["image", "rm", name]
                )
                self.command(arguments)
            except (AuditFailure, OSError, ValueError, KeyError, subprocess.SubprocessError):
                self.cleanup_ok = False


def main() -> int:
    """Run the isolated audit and emit only static status/stage names and public counts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="arkham:local")
    parser.add_argument("--docker", default="docker")
    args = parser.parse_args()
    docker = shutil.which(args.docker)
    if docker is None:
        print(json.dumps({"status": "failed", "stage": "docker_unavailable"}))
        return 1
    audit = ContextAudit(docker, args.image)
    summary = None
    try:
        summary = audit.run()
    except KeyboardInterrupt:
        audit.stage = "interrupted"
    except Exception:
        # The summary must not expose captured command output or source contents.
        pass
    finally:
        audit.cleanup()
    if summary is None or not audit.cleanup_ok:
        print(
            json.dumps(
                {"status": "failed", "stage": audit.stage, "cleanup_completed": audit.cleanup_ok}
            )
        )
        return 1
    print(json.dumps({"status": "passed", **summary, "cleanup_completed": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
