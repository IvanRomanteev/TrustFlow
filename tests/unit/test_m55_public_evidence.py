"""Public historical evidence must reject corrupted accounting and bindings."""

from copy import deepcopy

import pytest
from scripts.m55_public_evidence import DEFAULT, read, verify


def test_public_evidence_recomputes():
    assert verify(read(DEFAULT / "evidence.json")) == {
        "calls": 11,
        "input_tokens": 11042,
        "output_tokens": 11299,
        "estimated_cost_usd": "0.07320100",
        "sqlite_projections": 3,
        "spark_cases": 5,
    }


@pytest.mark.parametrize(
    "fault",
    [
        "tokens",
        "model",
        "cost",
        "http",
        "duplicate",
        "ledger",
        "snapshot",
        "wonder",
        "case",
        "budget",
    ],
)
def test_corrupted_evidence_is_rejected(fault):
    data = deepcopy(read(DEFAULT / "evidence.json"))
    stage = data["stages"][0]
    call = stage["calls"][0]
    if fault == "tokens":
        call["output_tokens"] += 1
    elif fault == "model":
        call["wire"]["response_model"] = "muse-wonder-1.0"
    elif fault == "cost":
        call["estimated_cost_usd"] = 0
    elif fault == "http":
        call["http_status"] = 503
    elif fault == "duplicate":
        stage["calls"][1]["response_id_sha256"] = call["response_id_sha256"]
    elif fault == "ledger":
        stage["sqlite"]["transactions"][0]["operation_id"] = "different-operation"
    elif fault == "snapshot":
        run = next(r for r in stage["sqlite"]["runs"] if r["approved_canonical"])
        run["approved_canonical"]["version"] += 1
    elif fault == "wonder":
        data["stages"][-1]["planning"] = "live Wonder planning"
    elif fault == "case":
        stage["cases"][1]["status"] = "REJECTED"
    else:
        data["configuration"]["advisory_limit_usd"] = 1000
    with pytest.raises(AssertionError):
        verify(data)
