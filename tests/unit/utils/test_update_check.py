"""A newer release is noticed (once a day at most), never in a dev install."""
import json
import time

import pytest

from code_visualizer.utils import update_check


@pytest.fixture
def check(tmp_path, monkeypatch):
    monkeypatch.setenv("CVIZ_UPDATE_CHECK_FILE", str(tmp_path / "update-check.json"))
    monkeypatch.delenv("CVIZ_NO_UPDATE_CHECK", raising=False)
    calls = []

    def fetch(timeout):
        calls.append(timeout)
        return fetch.answer

    fetch.answer = {"latest": "0.9.9", "url": "https://github.com/polysok/code-visualizer/releases/tag/v0.9.9"}
    monkeypatch.setattr(update_check, "_fetch", fetch)
    return fetch, calls, tmp_path / "update-check.json"


def test_versions_are_compared_as_numbers():
    assert update_check.parse_version("v0.9.10") > update_check.parse_version("0.9.9")
    assert update_check.parse_version("0.0.0 (dev)") is None
    assert update_check.parse_version(None) is None


def test_a_newer_release_is_reported(check):
    found = update_check.update_available("0.9.8")
    assert found == {"current": "0.9.8", "latest": "0.9.9", "url": check[0].answer["url"]}


def test_the_same_or_an_older_release_is_not(check):
    assert update_check.update_available("0.9.9") is None
    assert update_check.update_available("1.0.0") is None


def test_github_is_asked_once_a_day(check):
    fetch, calls, _ = check
    update_check.update_available("0.9.8")
    update_check.update_available("0.9.8")
    assert len(calls) == 1


def test_a_stale_answer_is_refreshed(check):
    fetch, calls, cache = check
    cache.write_text(json.dumps({"latest": "0.9.8", "checked_at": time.time() - 2 * 86400}))
    assert update_check.update_available("0.9.8")["latest"] == "0.9.9"
    assert len(calls) == 1


def test_offline_keeps_the_last_answer_and_waits_a_day(check):
    fetch, calls, cache = check
    cache.write_text(json.dumps({"latest": "0.9.9", "url": "u", "checked_at": time.time() - 2 * 86400}))
    fetch.answer = None  # offline
    assert update_check.update_available("0.9.8")["latest"] == "0.9.9"
    update_check.update_available("0.9.8")
    assert len(calls) == 1  # the failed check counts as today's


def test_a_dev_install_or_the_opt_out_never_asks(check, monkeypatch):
    fetch, calls, _ = check
    assert update_check.update_available("0.0.0 (dev)") is None
    monkeypatch.setenv("CVIZ_NO_UPDATE_CHECK", "1")
    assert update_check.update_available("0.9.8") is None
    assert calls == []


def test_the_page_asks_the_server(check, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_visualizer.viz.graph_api import router

    monkeypatch.setattr("code_visualizer.utils.package_info.installed_version", lambda: "0.9.8")
    app = FastAPI()
    app.include_router(router)
    body = TestClient(app).get("/api/update").json()
    assert body["available"] is True and body["latest"] == "0.9.9" and body["current"] == "0.9.8"
