from __future__ import annotations

import json
from decimal import Decimal

import pytest

from coding_agent.cost import CostTracker, PricingCatalog
from coding_agent.errors import AgentApiError, AgentError, BudgetExceeded
from coding_agent.openai_agent import ResponsesAgent
from tests.conftest import (
    FakeFunctionCall,
    FakeOpenAIClient,
    FakeResponse,
)


class FakeExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    def execute(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        if self.fail:
            raise AgentError("unknown tool")
        return "tool result"


def _tracker(max_cost: str = "2.00", reasoning: str | None = None) -> CostTracker:
    return CostTracker(
        PricingCatalog.load(),
        "gpt-5.4",
        max_cost,
        reasoning_effort=reasoning,
        echo=lambda _text: None,
    )


def _agent(client, tracker, *, max_steps: int = 5, reasoning=None) -> ResponsesAgent:
    return ResponsesAgent(
        client,
        model="gpt-5.4",
        instructions="system instructions",
        cost=tracker,
        max_steps=max_steps,
        max_output_tokens=1000,
        reasoning_effort=reasoning,
        echo=lambda _text: None,
    )


def test_function_call_loop_and_usage() -> None:
    call = FakeFunctionCall(
        name="list_files", arguments=json.dumps({"filter": ""}), call_id="call_abc"
    )
    client = FakeOpenAIClient(
        [
            FakeResponse(output=[call], output_text=""),
            FakeResponse(output=[], output_text="final answer"),
        ]
    )
    executor = FakeExecutor()
    tracker = _tracker()
    result = _agent(client, tracker).run(
        [{"role": "user", "content": "do work"}],
        [{"type": "function", "name": "list_files"}],
        executor,
        phase="analysis",
    )
    assert result.completed
    assert result.text == "final answer"
    assert result.steps == 2
    assert executor.calls == [("list_files", {"filter": ""})]
    assert any(
        isinstance(message, dict)
        and message.get("type") == "function_call_output"
        and message.get("call_id") == "call_abc"
        and message.get("output") == "tool result"
        for message in result.messages
    )
    assert tracker.calls == 2
    assert tracker.total_usage.input_tokens == 200
    assert client.responses.calls[0]["store"] is False
    assert client.responses.calls[0]["model"] == "gpt-5.4"
    assert client.responses.calls[0]["max_output_tokens"] == 1000


def test_reasoning_effort_is_passed() -> None:
    client = FakeOpenAIClient([FakeResponse(output=[], output_text="ok")])
    tracker = _tracker(reasoning="medium")
    _agent(client, tracker, reasoning="medium").run(
        [{"role": "user", "content": "x"}], [], FakeExecutor(), phase="analysis"
    )
    assert client.responses.calls[0]["reasoning"] == {"effort": "medium"}


def test_budget_blocks_before_api_call() -> None:
    client = FakeOpenAIClient([FakeResponse(output=[], output_text="never")])
    tracker = _tracker(max_cost="0.0001")
    with pytest.raises(BudgetExceeded):
        _agent(client, tracker).run(
            [{"role": "user", "content": "x"}],
            [],
            FakeExecutor(),
            phase="analysis",
        )
    assert client.responses.calls == []


def test_api_error_is_wrapped_and_redacted() -> None:
    key = "sk-" + "x" * 40

    class BrokenResponses:
        def create(self, **kwargs):
            raise RuntimeError(f"network failed key={key}")

    class BrokenClient:
        responses = BrokenResponses()

    tracker = _tracker()
    with pytest.raises(AgentApiError) as exc:
        _agent(BrokenClient(), tracker).run(
            [{"role": "user", "content": "x"}],
            [],
            FakeExecutor(),
            phase="analysis",
        )
    message = str(exc.value)
    assert key not in message
    assert "[REDACTED:openai-key]" in message


def test_incomplete_response_stops_without_tools() -> None:
    call = FakeFunctionCall(name="list_files", arguments="{}")
    client = FakeOpenAIClient(
        [
            FakeResponse(
                output=[call],
                output_text="partial",
                status="incomplete",
            )
        ]
    )
    executor = FakeExecutor()
    result = _agent(client, _tracker()).run(
        [{"role": "user", "content": "x"}], [], executor, phase="analysis"
    )
    assert not result.completed
    assert "incomplete" in result.reason
    assert executor.calls == []


def test_unknown_tool_becomes_tool_error_without_crash() -> None:
    call = FakeFunctionCall(name="shell", arguments="{}")
    client = FakeOpenAIClient(
        [FakeResponse(output=[call], output_text=""), FakeResponse(output=[], output_text="done")]
    )
    executor = FakeExecutor(fail=True)
    result = _agent(client, _tracker()).run(
        [{"role": "user", "content": "x"}],
        [],
        executor,
        phase="analysis",
    )
    assert result.completed
    outputs = [
        message["output"]
        for message in result.messages
        if isinstance(message, dict) and message.get("type") == "function_call_output"
    ]
    assert outputs and "Tool error" in outputs[0]


def test_max_steps_marks_run_incomplete() -> None:
    calls = [
        FakeFunctionCall(name="list_files", arguments="{}", call_id=f"c{i}")
        for i in range(3)
    ]
    client = FakeOpenAIClient([FakeResponse(output=[call]) for call in calls])
    result = _agent(client, _tracker(), max_steps=2).run(
        [{"role": "user", "content": "x"}], [], FakeExecutor(), phase="analysis"
    )
    assert not result.completed
    assert "maximum" in result.reason


def test_empty_final_text_is_not_complete() -> None:
    client = FakeOpenAIClient([FakeResponse(output=[], output_text="")])
    result = _agent(client, _tracker()).run(
        [{"role": "user", "content": "x"}], [], FakeExecutor(), phase="analysis"
    )
    assert not result.completed
    assert "no final text" in result.reason


def test_missing_usage_warns_and_uses_estimate() -> None:
    client = FakeOpenAIClient(
        [FakeResponse(output=[], output_text="ok", usage=None)]
    )
    tracker = _tracker()
    result = _agent(client, tracker).run(
        [{"role": "user", "content": "x"}], [], FakeExecutor(), phase="analysis"
    )
    assert result.completed
    assert tracker.calls == 1
    assert tracker.total_cost > Decimal(0)
