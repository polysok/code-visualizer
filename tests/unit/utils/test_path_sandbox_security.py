from pathlib import Path

import pytest

from code_visualizer.utils.path_sandbox import (
    clamp_discovery_depth,
    is_path_allowed,
    is_safe_download_url,
    sanitize_bundle_filename,
)


def test_sanitize_bundle_filename_rejects_traversal():
    # Path traversal sequences must always fall back to the default name.
    assert sanitize_bundle_filename("../../etc/passwd.cviz") == "bundle.cviz"
    assert sanitize_bundle_filename("numpy.cviz") == "numpy.cviz"


# Parametrized cases from the original issue report.
# Previously the second case was a bug: a .cviz-suffixed name with unsafe
# characters would skip sanitization entirely and be returned unchanged.
@pytest.mark.parametrize(
    "input_name, expected",
    [
        # Clean name - must pass through unchanged.
        ("ok.cviz", "ok.cviz"),
        # Unsafe chars present, already ends with .cviz - must be sanitized (was the bug).
        ("we ird$.cviz", "we_ird_.cviz"),
        # Unsafe chars present, non-.cviz extension - must be sanitized and .cviz appended.
        ("we ird$.txt", "we_ird_.txt.cviz"),
        # Injection payload containing a forward slash - the early guard rejects it
        # before sanitization and returns the safe default name.
        ("<script>alert(1)</script>.cviz", "bundle.cviz"),
        # Injection payload without slashes - unsafe chars are removed by the regex.
        # Parentheses are not in [\w.\-] so they must be replaced with underscores.
        ("alert(1).cviz", "alert_1_.cviz"),
    ],
)
def test_sanitize_bundle_filename_unconditional(input_name, expected):
    # Sanitization must run regardless of whether the filename ends with .cviz.
    assert sanitize_bundle_filename(input_name) == expected


def test_is_safe_download_url_allows_registry_hosts():
    assert is_safe_download_url(
        "https://huggingface.co/datasets/codegraphcontext/registry/resolve/main/foo.cviz"
    )
    assert not is_safe_download_url("http://evil.com/bundle.cviz")
    assert not is_safe_download_url("https://169.254.169.254/latest/meta-data")


def test_clamp_discovery_depth():
    assert clamp_discovery_depth(1000) == 10
    assert clamp_discovery_depth(-3) == 0
    assert clamp_discovery_depth("2") == 2


def test_is_path_allowed_rejects_outside_root_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CVIZ_ALLOW_ALL_PATHS", raising=False)
    monkeypatch.delenv("CVIZ_ALLOWED_ROOTS", raising=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    assert not is_path_allowed(outside)


def test_is_path_allowed_bypasses_sandbox_with_allow_all_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("CVIZ_ALLOWED_ROOTS", raising=False)
    monkeypatch.setenv("CVIZ_ALLOW_ALL_PATHS", "true")
    outside = tmp_path / "outside"
    outside.mkdir()
    assert is_path_allowed(outside)


def test_is_path_allowed_bypasses_sandbox_with_wildcard_allowed_roots(tmp_path, monkeypatch):
    monkeypatch.delenv("CVIZ_ALLOW_ALL_PATHS", raising=False)
    monkeypatch.setenv("CVIZ_ALLOWED_ROOTS", "*")
    outside = tmp_path / "outside"
    outside.mkdir()
    assert is_path_allowed(outside)


def test_is_path_allowed_still_allows_cwd_by_default(monkeypatch):
    monkeypatch.delenv("CVIZ_ALLOW_ALL_PATHS", raising=False)
    monkeypatch.delenv("CVIZ_ALLOWED_ROOTS", raising=False)
    assert is_path_allowed(Path.cwd())
