"""Credential-source boundaries and safe failure paths for production secret files."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from outage_explorer.runtime_secrets import SecretConfigurationError, read_secret

NAME = "EIA_API_KEY"


@pytest.fixture(autouse=True)
def isolated_sources(monkeypatch):
    monkeypatch.delenv(NAME, raising=False)
    monkeypatch.delenv(f"{NAME}_FILE", raising=False)
    monkeypatch.delenv("OUTAGE_SECRET_FILES_ONLY", raising=False)


def test_local_environment_and_missing_optional_value_remain_supported(monkeypatch):
    assert read_secret(NAME) == ""
    monkeypatch.setenv(NAME, "  synthetic-local-credential  ")
    assert read_secret(NAME, required=True) == "  synthetic-local-credential  "


def test_production_reads_only_file_and_preserves_exact_secret_bytes(tmp_path, monkeypatch):
    secret = tmp_path / "key"
    secret.write_text("  synthetic-file-credential  ", encoding="utf-8")
    monkeypatch.setenv("OUTAGE_SECRET_FILES_ONLY", "1")
    monkeypatch.setenv(f"{NAME}_FILE", str(secret))
    assert read_secret(NAME, required=True) == "  synthetic-file-credential  "
    assert NAME not in os.environ
    secret.write_text("rotated-synthetic-credential", encoding="utf-8")
    assert read_secret(NAME, required=True) == "rotated-synthetic-credential"


@pytest.mark.parametrize("configured_file", [False, True])
def test_production_rejects_value_environment_even_with_valid_file(
    tmp_path, monkeypatch, configured_file
):
    secret = tmp_path / "private-location-canary"
    secret.write_text("synthetic-file-canary", encoding="utf-8")
    monkeypatch.setenv("OUTAGE_SECRET_FILES_ONLY", "1")
    monkeypatch.setenv(NAME, "synthetic-environment-canary")
    if configured_file:
        monkeypatch.setenv(f"{NAME}_FILE", str(secret))
    with pytest.raises(SecretConfigurationError) as failure:
        read_secret(NAME, required=True)
    assert "canary" not in str(failure.value)
    assert str(secret) not in str(failure.value)


@pytest.mark.parametrize(
    "bad_content", [b"", b" \n\t", b"synthetic\x00canary", b"\xff", b"x" * 16_385]
)
def test_invalid_configured_file_fails_without_exposing_values_or_paths(
    tmp_path, monkeypatch, bad_content
):
    secret = tmp_path / "private-file-name-canary"
    secret.write_bytes(bad_content)
    monkeypatch.setenv(f"{NAME}_FILE", str(secret))
    with pytest.raises(SecretConfigurationError) as failure:
        read_secret(NAME)
    assert str(secret) not in str(failure.value)
    assert "canary" not in str(failure.value)
    assert failure.value.__suppress_context__ or failure.value.__context__ is None


@pytest.mark.parametrize("file_kind", ["missing", "directory", "symlink"])
def test_unreadable_or_nonregular_file_is_never_an_environment_fallback(
    tmp_path, monkeypatch, file_kind
):
    secret = tmp_path / "unsafe-file-canary"
    if file_kind == "directory":
        secret.mkdir()
    elif file_kind == "symlink":
        target = tmp_path / "target"
        target.write_text("synthetic-target-canary", encoding="utf-8")
        try:
            secret.symlink_to(target)
        except OSError:
            pytest.skip("This Windows account cannot create symbolic links.")
    monkeypatch.setenv(f"{NAME}_FILE", str(secret))
    with pytest.raises(SecretConfigurationError) as failure:
        read_secret(NAME)
    assert "canary" not in str(failure.value)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO boundary")
def test_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
    secret = tmp_path / "fifo"
    os.mkfifo(secret)
    environment = dict(os.environ, EIA_API_KEY_FILE=str(secret))
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "from outage_explorer.runtime_secrets import read_secret; read_secret('EIA_API_KEY')",
        ],
        env=environment,
        text=True,
        capture_output=True,
        timeout=3,
    )
    assert result.returncode != 0 and "could not be read securely" in result.stderr
    assert str(secret) not in result.stderr


def test_missing_required_file_and_invalid_mode_fail_closed(monkeypatch):
    monkeypatch.setenv("OUTAGE_SECRET_FILES_ONLY", "1")
    assert read_secret(NAME) == ""
    with pytest.raises(SecretConfigurationError, match="EIA_API_KEY is required"):
        read_secret(NAME, required=True)
    monkeypatch.setenv("OUTAGE_SECRET_FILES_ONLY", "invalid-mode-canary")
    with pytest.raises(SecretConfigurationError) as failure:
        read_secret(NAME)
    assert "canary" not in str(failure.value)


def test_invalid_environment_text_cannot_escape_in_an_encoding_error(monkeypatch):
    # POSIX environ can contain surrogate-escaped bytes; reject them with a safe error.
    if os.name == "nt":
        pytest.skip("Windows environment cannot store surrogate-escaped text.")
    monkeypatch.setenv(NAME, "synthetic-canary-\udcff")
    with pytest.raises(SecretConfigurationError) as failure:
        read_secret(NAME)
    assert "canary" not in str(failure.value)
