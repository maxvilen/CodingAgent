from __future__ import annotations

import json

import httpx2
from openai import OpenAI

from coding_agent.cost import CostTracker, PricingCatalog
from coding_agent.openai_agent import ResponsesAgent


class Executor:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, name, args):
        self.calls.append((name, args))
        return "tool result"


def _response_payload(output, text=None):
    payload = {
        "id": "resp_test",
        "object": "response",
        "created_at": 1.0,
        "model": "gpt-5.4",
        "status": "completed",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_tokens_details": {
                "cached_tokens": 10,
                "cache_write_tokens": 0,
            },
            "output_tokens_details": {"reasoning_tokens": 5},
        },
    }
    if text is not None:
        payload["output"] = [
            {
                "type": "message",
                "id": "msg_test",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
            }
        ]
    return payload


def test_real_sdk_serializes_function_call_loop() -> None:
    requests: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx2.Response(
                200,
                json=_response_payload(
                    [
                        {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_sdk",
                            "name": "list_files",
                            "arguments": json.dumps({"filter": ""}),
                            "status": "completed",
                        }
                    ]
                ),
            )
        return httpx2.Response(200, json=_response_payload([], text="finished"))

    client = OpenAI(
        api_key="sk-test-not-real",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    tracker = CostTracker(
        PricingCatalog.load(), "gpt-5.4", "2.00", echo=lambda _text: None
    )
    executor = Executor()
    agent = ResponsesAgent(
        client,
        model="gpt-5.4",
        instructions="system",
        cost=tracker,
        max_steps=3,
        max_output_tokens=1000,
        echo=lambda _text: None,
    )
    result = agent.run(
        [{"role": "user", "content": "inspect"}],
        [{"type": "function", "name": "list_files"}],
        executor,
        phase="analysis",
    )
    assert result.completed
    assert result.text == "finished"
    assert executor.calls == [("list_files", {"filter": ""})]
    assert tracker.calls == 2
    assert tracker.total_usage.cached_tokens == 20
    assert requests[0]["store"] is False
    assert requests[0]["max_output_tokens"] == 1000
    assert requests[0]["model"] == "gpt-5.4"
    assert len(requests) == 2
    # The second request contains the function call output, proving the SDK
    # accepted the mixed response-object/dict input format used by the agent.
    second_input = requests[1]["input"]
    assert any(
        item.get("type") == "function_call_output"
        and item.get("call_id") == "call_sdk"
        and item.get("output") == "tool result"
        for item in second_input
        if isinstance(item, dict)
    )
