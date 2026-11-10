"""Small deterministic execution primitives for ecommerce tool workflows.

The audit emitted here is intentionally a public business-process summary.  It
does not contain hidden model reasoning, raw tool payloads, or sensitive tool
arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class ReActAction:
    """One bounded tool action and its user-safe rationale."""

    tool: str
    arguments: dict
    thought_summary: str


@dataclass(frozen=True)
class ReActDecision:
    """Deterministic decision made after observing a tool result."""

    next_action: ReActAction | None = None
    final_message: str | None = None


@dataclass
class ReActOutcome:
    """Result of a bounded mini-ReAct run."""

    executions: list[tuple[ReActAction, dict]] = field(default_factory=list)
    audit: list[str] = field(default_factory=list)
    final_message: str = ""
    stopped_on_failure: bool = False
    reached_step_limit: bool = False

    @property
    def final_result(self) -> dict:
        return self.executions[-1][1] if self.executions else {}


class MiniReActExecutor:
    """Run a deterministic observe/decide loop with a hard step limit.

    ``decide_next`` is normal application code, not an LLM.  Tool failures stop
    immediately so a failed authorization/ownership check can never authorize a
    later tool call.
    """

    def __init__(self, registry, *, max_steps: int = 2) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.registry = registry
        self.max_steps = max_steps

    def run(
        self,
        initial_action: ReActAction,
        *,
        decide_next: Callable[[ReActAction, dict], ReActDecision],
        summarize_observation: Callable[[ReActAction, dict], str],
    ) -> ReActOutcome:
        outcome = ReActOutcome()
        action: ReActAction | None = initial_action

        for _ in range(self.max_steps):
            if action is None:
                return outcome

            outcome.audit.append(f"Thought: {action.thought_summary}")
            outcome.audit.append(f"Action: {action.tool}")
            try:
                result = self.registry.execute(action.tool, **action.arguments)
            except Exception:
                # Keep the public audit stable and avoid exposing stack traces,
                # credentials, or provider-specific error details.
                result = {
                    "ok": False,
                    "error": "tool_exception",
                    "message": "工具执行异常，流程已停止，请稍后重试或转人工。",
                }
            outcome.executions.append((action, result))
            outcome.audit.append(f"Observation: {summarize_observation(action, result)}")

            if not result.get("ok"):
                outcome.stopped_on_failure = True
                outcome.final_message = result.get("message") or "工具执行失败，流程已停止。"
                return outcome

            decision = decide_next(action, result)
            if decision.final_message is not None:
                outcome.final_message = decision.final_message
                return outcome
            action = decision.next_action

        if action is not None:
            outcome.reached_step_limit = True
            outcome.audit.append(
                f"Observation: 已达到 {self.max_steps} 步执行上限，未继续调用工具"
            )
            outcome.final_message = "查询已达到安全执行上限，请转接人工继续核对。"
        return outcome
