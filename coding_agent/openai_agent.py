"""OpenAI Responses API loop with explicit tools and cost checks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from openai import OpenAI

from .cost import CostTracker
from .errors import AgentApiError
from .security import clip, redact_secrets, safe_for_terminal


@dataclass
class AgentLoopResult:
    text: str
    completed: bool
    steps: int
    messages: list[Any]
    reason: str = ""


class ResponsesAgent:
    def __init__(
        self,
        client: OpenAI,
        *,
        model: str,
        instructions: str,
        cost: CostTracker,
        max_steps: int = 18,
        max_output_tokens: int = 8_000,
        reasoning_effort: str | None = None,
        echo: Callable[[str], None] = print,
    ) -> None:
        self.client = client
        self.model = model
        self.instructions = instructions
        self.cost = cost
        self.max_steps = max_steps
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.echo = echo

    def run(
        self,
        messages: list[Any],
        tools: Sequence[dict[str, Any]],
        executor: Any,
        *,
        phase: str,
    ) -> AgentLoopResult:
        for step in range(1, self.max_steps + 1):
            self.cost.prepare_call(
                self.instructions,
                messages,
                label=f"{phase}-step-{step}",
                max_output_tokens=self.max_output_tokens,
            )
            kwargs: dict[str, Any] = {
                "model": self.model,
                "instructions": self.instructions,
                "input": messages,
                "tools": list(tools),
                "store": False,
                "max_output_tokens": self.max_output_tokens,
            }
            if self.reasoning_effort:
                kwargs["reasoning"] = {"effort": self.reasoning_effort}
            try:
                response = self.client.responses.create(**kwargs)
            except Exception as exc:
                detail = safe_for_terminal(redact_secrets(str(exc)), 1_500)
                raise AgentApiError(
                    f"OpenAI API call failed ({type(exc).__name__}): {detail}"
                ) from exc

            usage = getattr(response, "usage", None)
            self.cost.record_usage(usage, label=f"{phase}-step-{step}")

            status = getattr(response, "status", None) or "completed"
            output = list(getattr(response, "output", None) or [])
            messages.extend(output)
            if status != "completed":
                reason = f"API response status: {status}"
                details = getattr(response, "incomplete_details", None)
                if details is not None:
                    reason += f" ({details})"
                text = _output_text(response)
                return AgentLoopResult(
                    text=text,
                    completed=False,
                    steps=step,
                    messages=messages,
                    reason=safe_for_terminal(reason, 1_000),
                )

            calls = [
                item
                for item in output
                if getattr(item, "type", None) == "function_call"
            ]
            if not calls:
                text = _output_text(response)
                if not text.strip():
                    return AgentLoopResult(
                        text="",
                        completed=False,
                        steps=step,
                        messages=messages,
                        reason="The model returned no final text.",
                    )
                return AgentLoopResult(
                    text=safe_for_terminal(text, 40_000),
                    completed=True,
                    steps=step,
                    messages=messages,
                )

            for call in calls:
                name = str(getattr(call, "name", ""))
                call_id = str(getattr(call, "call_id", ""))
                arguments = getattr(call, "arguments", "") or "{}"
                try:
                    if isinstance(arguments, dict):
                        parsed_args = arguments
                    else:
                        parsed_args = json.loads(arguments)
                    if not isinstance(parsed_args, dict):
                        raise ValueError("arguments must be a JSON object")
                    result = executor.execute(name, parsed_args)
                    display = safe_for_terminal(redact_secrets(str(result)), 400)
                    self.echo(f"[{name}] {display}")
                except Exception as exc:
                    message = str(exc) or type(exc).__name__
                    result = (
                        f"Tool error ({type(exc).__name__}): "
                        f"{safe_for_terminal(redact_secrets(message), 2_000)}"
                    )
                    self.echo(f"[{name}] {safe_for_terminal(result, 400)}")
                output_text = clip(str(result), 12_000)
                messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output_text,
                    }
                )

        return AgentLoopResult(
            text="",
            completed=False,
            steps=self.max_steps,
            messages=messages,
            reason=(
                f"Stopped after the maximum of {self.max_steps} model/tool steps. "
                "Review the worktree before continuing."
            ),
        )


def _output_text(response: Any) -> str:
    try:
        value = getattr(response, "output_text", "") or ""
    except Exception:
        value = ""
    if isinstance(value, str):
        return value
    return str(value)


__all__ = ["AgentLoopResult", "ResponsesAgent"]
