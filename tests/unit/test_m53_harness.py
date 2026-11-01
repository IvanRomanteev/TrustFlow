"""Bounded evaluation, authorization flags, SQLite observations and comparisons."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from trustflow.core.settings import Settings
from trustflow.evaluation.live import (
    EvaluationSettings,
    evaluate_profile,
    main,
    model_metrics,
    offline_transport,
    sql_evidence,
)
from trustflow.llm.provider import MetaResponsesProvider


def test_dry_plan_needs_no_key_or_http(monkeypatch, capsys):
    monkeypatch.delenv("MODEL_API_KEY")
    assert main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "plan_only" and report["paid_requests_executed"] == 0
    assert report["expected_requests_per_profile"] == 9


@pytest.mark.parametrize("mode", ["fake", "live"])
def test_plan_only_does_not_initialize_standard_app_or_demo_storage(tmp_path, mode):
    trap = tmp_path / "ordinary-demo.db"
    (tmp_path / ".env").write_text(f"TRUSTFLOW_LLM_MODE={mode}\nTRUSTFLOW_DB_PATH={trap}\n")
    env = {name: os.environ[name] for name in os.environ
           if name not in {"MODEL_API_KEY", "TRUSTFLOW_LLM_MODE", "TRUSTFLOW_DB_PATH"}}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run([sys.executable, "-m", "trustflow.evaluation.live"],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["paid_requests_executed"] == 0
    assert not trap.exists()


def test_fixture_settings_ignore_ambient_demo_configuration(monkeypatch):
    monkeypatch.setenv("TRUSTFLOW_DB_PATH", "ordinary-demo.db")
    monkeypatch.setenv("TRUSTFLOW_HIGH_RISK_THRESHOLD", "999999")
    monkeypatch.setenv("TRUSTFLOW_LLM_MODE", "live")
    settings = EvaluationSettings()
    assert settings.db_path is None and settings.llm_mode == "fake"
    assert settings.high_risk_threshold == 1000


@pytest.mark.parametrize("args", [["--live"], ["--authorize-paid"],
    ["--offline", "--authorize-paid"], ["--max-requests", "0"],
    ["--max-cost-usd", "nan"], ["--max-cost-usd", "inf"], ["--retries", "4"]])
def test_paid_flag_and_input_validation(args):
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2


def test_paid_flags_without_environment_key_fail_before_inference(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY")
    with pytest.raises(SystemExit):
        main(["--live", "--authorize-paid"])


def test_full_offline_matrix_and_fake_comparison(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "opaque-offline-sentinel-credential")
    assert main(["--offline", "--profile", "all", "--max-requests", "27",
                 "--max-cost-usd", "1", "--out", str(tmp_path)]) == 0
    info = json.loads(capsys.readouterr().out)
    path = Path(info["report"])
    report = json.loads(path.read_text())
    assert report["http_attempts"] == 27 and report["paid_requests_executed"] == 0
    assert report["mode"] == "offline_mock" and report["passed"]
    assert report["fake_comparator"]["task_completion"] == 4
    assert len(report["comparison"]) == 15
    for profile in report["profiles"]:
        assert profile["task_completion"] == profile["task_count"] == 5
        recovery = next(c for c in profile["cases"] if c["case"] == "recovery")
        assert recovery["checks"]["verification_only"]
        assert len([r for r in recovery["calls"] if r["purpose"] == "semantic_review"]) == 2
        assert any(r["failure_reason"] == "injected_invalid_review" for r in recovery["calls"])
        txs = sql_evidence(path.parent / profile["profile"] / "synthetic.db")["transactions"]
        assert [t["amount"] for t in txs] == [1500, 1200]
    assert report["model_metrics"]["muse-wonder-1.0"]["http_attempts"] == 14
    assert report["model_metrics"]["muse-spark-1.3"]["http_attempts"] == 13
    for artifact in path.parent.rglob("*.json"):
        assert "opaque-offline-sentinel-credential" not in artifact.read_text()
    assert os.environ["MODEL_API_KEY"] == "opaque-offline-sentinel-credential"


def test_request_cap_stops_matrix_and_retains_failure(tmp_path, capsys):
    assert main(["--offline", "--profile", "all", "--max-requests", "1",
                 "--out", str(tmp_path)]) == 1
    report = json.loads(Path(json.loads(capsys.readouterr().out)["report"]).read_text())
    assert not report["passed"] and report["http_attempts"] == 1
    assert len(report["profiles"]) == 1
    assert report["profiles"][0]["cases"][0]["status"] == "VERIFICATION_PENDING"
    assert "limit exhausted" in report["profiles"][0]["cases"][0]["failure_reason"]
    assert report["model_metrics"]["muse-spark-1.3"]["http_attempts"] == 1


async def test_changed_model_payment_is_not_auto_approved(tmp_path):
    base = offline_transport()

    def handle(request):
        response = base.handle_request(request)
        if json.loads(request.content)["text"]["format"]["name"] == "plan_steps":
            body = response.json()
            body["output"][0]["content"][0]["text"] = json.dumps({"steps": [{
                "tool_name": "simulate_transfer", "from_account": "acc_alice",
                "to_account": "acc_bob", "amount": 1499, "currency": "USD"}]})
            return httpx.Response(200, json=body)
        return response

    provider = MetaResponsesProvider(
        Settings(_env_file=None, planning_model="muse-spark-1.3", review_model="muse-spark-1.3"),
        transport=httpx.MockTransport(handle))
    result = await evaluate_profile(tmp_path / "bad-model", "spark", provider, "offline_mock")
    assert not result["passed"]
    assert not sql_evidence(tmp_path / "bad-model/synthetic.db")["transactions"]
    assert all(c["approved_hash"] == "" for c in result["cases"])


def test_unknown_usage_never_reported_as_known_zero_cost():
    metrics = model_metrics([{"model": "muse-spark-1.3", "usage_unknown": True,
                             "estimated_cost_usd": 0, "latency_ms": 1,
                             "validation_outcome": "failed", "failure_reason": "transport_error"}])
    assert metrics["muse-spark-1.3"]["estimated_total_cost_usd"] is None
    assert metrics["muse-spark-1.3"]["input_tokens"] is None


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose config requires Docker CLI")
def test_compose_live_is_separate_loopback_and_isolated():
    # docker compose interpolation sees a dummy key only. Capture and inspect
    # the rendered config in memory; never print or save environment values.
    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["MODEL_API_KEY"] = "offline-compose-placeholder"
    response = subprocess.run(
        ["docker", "compose", "--env-file", os.devnull, "-f", "docker-compose.live.yml",
         "config", "--format", "json"], cwd=root, env=env, capture_output=True, text=True)
    assert response.returncode == 0
    config = json.loads(response.stdout)
    assert config["name"] == "trustflow-m53-live"
    assert set(config["volumes"]) == {"m53-synthetic"}
    for service in config["services"].values():
        assert all(p["host_ip"] == "127.0.0.1" for p in service["ports"])
    backend = config["services"]["backend"]
    assert backend["environment"]["TRUSTFLOW_LLM_MODE"] == "live"
    assert backend["environment"]["MODEL_API_KEY"] == "offline-compose-placeholder"
    assert backend["volumes"][0]["target"] == "/synthetic"
