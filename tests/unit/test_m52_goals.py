"""The maintained read-only scenario must stay read-only under the real fake contract."""

from types import SimpleNamespace

import pytest
from scripts.m52 import runner
from scripts.m52.runner import READ_ONLY_GOAL

from trustflow.llm.provider import FakeLLMProvider


def test_demo_read_only_goal_never_proposes_a_mutation():
    steps, _ = FakeLLMProvider().propose_plan(
        READ_ONLY_GOAL["title"], READ_ONLY_GOAL["description"])
    assert [step["tool_name"] for step in steps] == ["get_balance"]


def test_windows_long_output_fails_before_docker_or_sqlite_creation(monkeypatch):
    monkeypatch.setattr(runner.os, "name", "nt")
    stack = SimpleNamespace(_stack=SimpleNamespace(marker_file="x" * 300))
    # No run(), Docker or browser seam exists on this object: path preflight
    # must fail before touching any of them or preparing a workspace.
    with pytest.raises(AssertionError, match="Windows MAX_PATH"):
        runner.prerequisites(stack)
