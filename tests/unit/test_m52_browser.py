"""Offline checks of browser evidence boundaries; no Chromium dependency."""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "m52" / "browser.py"
spec = importlib.util.spec_from_file_location("m52_browser_under_test", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
DemoBrowser = module.DemoBrowser


def test_browser_failure_log_redacts_token_and_url_query(tmp_path):
    browser = DemoBrowser("http://127.0.0.1:3001", "http://127.0.0.1:8001", tmp_path, "secret")

    class Request:
        method = "GET"
        url = "http://127.0.0.1:8001/health?token=secret#secret"
        failure = "connection failed: secret"

        @staticmethod
        def is_navigation_request():
            return False

    browser._request_failed(Request())
    browser.__exit__(None, None, None)
    saved = (tmp_path / "browser-log.json").read_text(encoding="utf-8")
    assert "secret" not in saved
    assert "?token" not in saved
    assert json.loads(saved)["events"][0]["url"] == "http://127.0.0.1:8001/health"
    with pytest.raises(AssertionError, match="unexpected browser errors"):
        browser.assert_clean()


def test_declared_backend_crash_allows_network_failure_but_never_page_error(tmp_path):
    browser = DemoBrowser("http://127.0.0.1:3001", "http://127.0.0.1:8001", tmp_path, "secret")
    browser.events.append({"kind": "requestfailed", "expected": True})
    browser.assert_clean()
    browser.events.append({"kind": "pageerror", "error": "JavaScript failed"})
    with pytest.raises(AssertionError, match="JavaScript failed"):
        browser.assert_clean()


@pytest.mark.parametrize("name", ["../escape", "nested/name", "C:\\other", "empty.png"])
def test_screenshot_name_cannot_escape_evidence_directory(tmp_path, name):
    browser = DemoBrowser("http://127.0.0.1:3001", "http://127.0.0.1:8001", tmp_path, "secret")
    with pytest.raises(ValueError, match="simple identifier"):
        browser.capture(name)


def test_amount_inspection_accepts_js_integer_format_and_rejects_other_payment():
    pattern = module._amount_pattern(5000.0, "USD")
    assert pattern.search("simulate_transfer acc_alice → acc_bob · 5000 USD")
    assert not pattern.search("simulate_transfer acc_alice → acc_bob · 50000 USD")
    assert not pattern.search("simulate_transfer acc_alice → acc_bob · 5000 EUR")
    assert module._amount_pattern(25.5, "USD").search("acc_alice · 25.5 USD")


@pytest.mark.parametrize("changed", [None, "status", "plan_hash", "approved_hash", "approvals"])
def test_reload_requires_same_persisted_decision_and_hash(tmp_path, monkeypatch, changed):
    expected = {"run_id": "run-one", "status": "REJECTED", "plan": {"plan_hash": "a" * 64},
                "approved_hash": "", "approvals": [{"decision": "REJECTED"}]}
    actual = copy.deepcopy(expected)
    if changed == "status":
        actual["status"] = "COMPLETED"
    elif changed == "plan_hash":
        actual["plan"]["plan_hash"] = "b" * 64
    elif changed == "approved_hash":
        actual["approved_hash"] = "b" * 64
    elif changed == "approvals":
        actual["approvals"] = []
    browser = DemoBrowser("http://127.0.0.1:3001", "http://127.0.0.1:8001", tmp_path, "secret")
    browser.page = MagicMock()
    monkeypatch.setattr(browser, "_expect", lambda locator: locator)
    monkeypatch.setattr(browser, "_read", lambda run_id: actual)
    checked = []
    monkeypatch.setattr(browser, "assert_run", checked.append)
    if changed is not None:
        with pytest.raises(AssertionError, match="reload changed"):
            browser.refresh(expected)
        assert checked == []
    else:
        assert browser.refresh(expected) == actual
        assert checked == [actual]
