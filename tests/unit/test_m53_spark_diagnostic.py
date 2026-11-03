"""Offline diagnostic contracts; fixtures are documented shapes, not captured Muse output."""

import json
from argparse import Namespace
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from trustflow.core.errors import LLMResponseError
from trustflow.core.settings import Settings
from trustflow.evaluation import diagnose
from trustflow.evaluation.live import EvaluationSettings
from trustflow.llm.provider import PLAN_SCHEMA, MetaResponsesProvider
from trustflow.llm.usage import UsageTracker

READ = {"steps": [{"tool_name": "get_balance", "account_id": "acc_alice",
                  "from_account": "", "to_account": "", "amount": 0, "currency": "",
                  "from_currency": "", "to_currency": "", "limit": 1, "rationale": "read"}]}
PRIVATE = "private-sentinel-not-for-artifacts"


def response(*, status="completed", text=None, output_tokens=250, reasoning_tokens=100):
    return {"model": "muse-spark-1.3", "status": status,
            "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete"
            else None,
            "output": [{"type": "reasoning", "summary": [{"text": PRIVATE}],
                        "encrypted_content": PRIVATE, "content": [
                            {"type": "reasoning_text", "text": PRIVATE}]},
                       {"type": "message", "role": "assistant", "status": status,
                        "content": [{"type": "output_text", "text": text or json.dumps(READ)}]}],
            "usage": {"input_tokens": 282, "output_tokens": output_tokens,
                      "output_tokens_details": {"reasoning_tokens": reasoning_tokens}}}


def provider(handler, **options):
    return MetaResponsesProvider(EvaluationSettings(
        llm_mode="live", planning_model="muse-spark-1.3", review_model="muse-spark-1.3",
        **options), transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("tracked", [False, True])
@pytest.mark.parametrize("shape", ["reasoning_only", "partial_json", "complete_json"])
def test_output_limit_never_accepts_incomplete_even_if_json_is_valid(tracked, shape):
    body = response(status="incomplete", output_tokens=1200, reasoning_tokens=1200)
    if shape == "reasoning_only":
        body["output"] = body["output"][:1]
    elif shape == "partial_json":
        body["output"][1]["content"][0]["text"] = '{"steps":['
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=body)

    p = provider(handle, llm_max_retries=3)
    tracker = UsageTracker(30000, "wf") if tracked else None
    with pytest.raises(LLMResponseError) as error:
        p.propose_plan("read", "", tracker=tracker)
    records = tracker.records if tracker else error.value.records
    assert count == p.budget.requests == len(records) == 1  # No invalid-response retry.
    rec = records[0]
    assert (rec.input_tokens, rec.output_tokens) == (282, 1200)
    assert rec.response_metadata.response_status == "incomplete"
    assert rec.response_metadata.incomplete_reason == "max_output_tokens"
    assert rec.response_metadata.reasoning_tokens == 1200
    assert rec.response_metadata.output_token_cap_reached
    assert rec.estimated_cost_usd > 0 and rec.budget_charge_usd == rec.estimated_cost_usd
    assert PRIVATE not in rec.model_dump_json() and PRIVATE not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("effort", [None, "minimal", "low"])
def test_documented_reasoning_then_assistant_shape_and_wire_contract(effort):
    def handle(request):
        body = json.loads(request.content)
        assert body["model"] == "muse-spark-1.3" and body["max_output_tokens"] == 4096
        assert body["text"]["format"] == {
            "type": "json_schema", "name": "plan_steps", "schema": PLAN_SCHEMA, "strict": True}
        assert body["store"] is False and "include" not in body and "tools" not in body
        assert body.get("reasoning") == ({"effort": effort} if effort else None)
        return httpx.Response(200, json=response())

    p = provider(handle, max_output_tokens=4096, llm_reasoning_effort=effort)
    raw, record = p.propose_plan("read", "")
    assert raw == READ["steps"]
    assert record.response_metadata.response_status == "completed"
    assert record.response_metadata.reasoning_tokens == 100
    assert [i.type for i in record.response_metadata.output_items] == ["reasoning", "message"]
    assert record.output_tokens == 250  # Reasoning is a subset, never added again.
    assert PRIVATE not in record.model_dump_json()


@pytest.mark.parametrize("body,category", [
    (response(text='{"steps":['), "json_syntax"),
    (response(text='{"steps":[],"steps":[]}'), "json_value"),
    ({"status": "completed", "choices": [{"message": {"content": PRIVATE}}]},
     "envelope_or_output"),
    (response(text='[1,2]'), "envelope_or_output"),
])
def test_metadata_distinguishes_syntax_and_unsupported_shape(body, category):
    p = provider(lambda req: httpx.Response(200, json=body), llm_max_retries=3)
    with pytest.raises(LLMResponseError) as error:
        p.propose_plan("read", "")
    assert p.budget.requests == 1
    meta = error.value.record.response_metadata
    assert meta.parse_failure == category
    assert PRIVATE not in meta.model_dump_json()


def test_untrusted_metadata_is_bounded_and_does_not_echo_strings():
    body = response()
    body["status"] = PRIVATE
    body["incomplete_details"] = {"reason": PRIVATE}
    body["usage"]["output_tokens_details"]["reasoning_tokens"] = True
    body["output"] = [{"type": PRIVATE, "role": PRIVATE, "status": PRIVATE,
                       "phase": PRIVATE, "content": [{"type": PRIVATE, "text": PRIVATE}]*30}]*30
    p = provider(lambda req: httpx.Response(200, json=body))
    with pytest.raises(LLMResponseError) as error:
        p.propose_plan("read", "")
    meta = error.value.record.response_metadata
    assert meta.response_status == meta.incomplete_reason == "other"
    assert meta.reasoning_tokens is None
    assert len(meta.output_items) == len(meta.output_items[0].content_types) == 16
    assert meta.output_items_count == 30 and meta.output_items_truncated
    assert PRIVATE not in meta.model_dump_json()


@pytest.mark.parametrize("effort", ["none", "high", "invalid", 1])
def test_effort_configuration_is_a_narrow_opt_in(effort):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_reasoning_effort=effort)


@pytest.mark.parametrize("kind", ["valid", "incomplete", "invalid_tool", "wrong_goal"])
def test_one_request_diagnostic_uses_fresh_storage_and_never_repairs(
        tmp_path, monkeypatch, capsys, kind):
    calls = []
    body = response()
    if kind == "incomplete":
        body = response(status="incomplete", output_tokens=4096, reasoning_tokens=4096)
    if kind == "invalid_tool":
        body = response(text=json.dumps({"steps": [{"tool_name": "foreign_tool"}]}))
    if kind == "wrong_goal":
        body = response(text=json.dumps({"steps": [{"tool_name": "simulate_transfer",
            "from_account": "acc_alice", "to_account": "acc_bob", "amount": 1500,
            "currency": "USD"}]}))

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    monkeypatch.setattr(diagnose, "offline_transport", lambda: httpx.MockTransport(handle))
    monkeypatch.setenv("MODEL_API_KEY", "opaque-sentinel-credential")
    monkeypatch.setenv("TRUSTFLOW_DB_PATH", str(tmp_path / "foreign.db"))
    result = diagnose.main(["--offline", "--out", str(tmp_path / "evidence")])
    assert result == (0 if kind == "valid" else 1)
    info = json.loads(capsys.readouterr().out)
    report_file = Path(info["report"])
    report = json.loads(report_file.read_text())
    assert len(calls) == report["http_attempts"] == 1
    assert report["repair_attempts"] == report["tool_executions"] == 0
    assert report["paid_requests_executed"] == 0 and not report["live_integration_pass"]
    assert report["ledger_before"] == report["ledger_after"]
    assert not report["ledger_after"]["transactions"]
    assert report["calls"][0]["response_metadata"]["reasoning_tokens"] is not None
    assert PRIVATE not in report_file.read_text()
    assert "opaque-sentinel-credential" not in report_file.read_text()
    assert not (tmp_path / "foreign.db").exists()
    assert (report_file.parent / "synthetic.db").is_file()


def test_diagnostic_budget_blocks_before_http(tmp_path, monkeypatch, capsys):
    def forbidden(request):
        pytest.fail("Budget must block HTTP")
    monkeypatch.setattr(diagnose, "offline_transport", lambda: httpx.MockTransport(forbidden))
    assert diagnose.main(["--offline", "--max-cost-usd", "0.000001",
                          "--out", str(tmp_path)]) == 1
    p = Path(json.loads(capsys.readouterr().out)["report"])
    assert json.loads(p.read_text())["http_attempts"] == 0


def test_diagnostic_plan_only_ignores_ambient_key_and_storage(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TRUSTFLOW_DB_PATH=foreign.db\n")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    assert diagnose.main([]) == 0
    assert json.loads(capsys.readouterr().out)["paid_requests_executed"] == 0
    assert not (tmp_path / "foreign.db").exists()
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize("args", [["--live"], ["--authorize-paid"],
    ["--offline", "--authorize-paid"], ["--profile", "wonder"], ["--max-requests", "2"],
    ["--retries", "1"], ["--reasoning-effort", "none"], ["--max-cost-usd", "nan"],
    ["--max-cost-usd", "0.06"], ["--max-output-tokens", "4097"]])
def test_diagnostic_authorization_and_fixed_limits_reject_before_inference(args):
    with pytest.raises(SystemExit) as error:
        diagnose.main(args)
    assert error.value.code == 2


def test_diagnostic_fresh_directory_guard(tmp_path):
    (tmp_path / "synthetic.db").write_bytes(b"foreign resource sentinel")
    args = Namespace(live=False, max_cost_usd=0.03, max_output_tokens=4096, reasoning_effort="low")
    with pytest.raises(ValueError, match="fresh"):
        diagnose.execute(args, tmp_path)
    assert (tmp_path / "synthetic.db").read_bytes() == b"foreign resource sentinel"
