"""The maintained read-only scenario must stay read-only under the real fake contract."""

from scripts.m52.runner import READ_ONLY_GOAL

from trustflow.llm.provider import FakeLLMProvider


def test_demo_read_only_goal_never_proposes_a_mutation():
    steps, _ = FakeLLMProvider().propose_plan(
        READ_ONLY_GOAL["title"], READ_ONLY_GOAL["description"])
    assert [step["tool_name"] for step in steps] == ["get_balance"]
