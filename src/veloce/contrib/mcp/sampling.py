"""Sampling with tools — driving the client's LLM through several rounds.

`MCPContext.sample` is one round trip: a prompt goes out, a completion comes
back. The modern revision lets that request carry tools the model may call, and
answer with a `tool_use` block instead of a completion — which leaves the
handler holding a request it must execute, append and send back, round after
round, before it has an answer.

`MCPContext.sample_with_tools` runs that loop. It declares tools this server
already has, executes the ones the model asks for through the same path
`tools/call` serves - scope checks, hooks, timeouts and error shaping included -
and feeds each result back as the next message. It returns a `SamplingRun`: the
answer, the transcript that produced it, and every tool call made along the way.

The model may only reach the tools the handler named. One it asks for outside
that set comes back as an error result rather than being executed, so declaring
a narrow set is a real restriction and not a hint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# What the model is told on the last allowed round: answer, do not ask for
# another tool. Without it a run that keeps requesting tools ends with an
# unanswered request instead of an answer.
_NO_MORE_TOOLS = {"mode": "none"}


@dataclass(frozen=True, slots=True)
class SampledToolCall:
    """One tool the model asked for during a run, and what it answered."""

    name: str
    id: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    is_error: bool


@dataclass(frozen=True, slots=True)
class SamplingRun:
    """The outcome of a `sample_with_tools` loop.

    `content` is the final assistant content, `messages` the whole transcript
    including that answer as its closing turn - so extending it for another run
    carries the reply along - and `tool_calls` every tool the model drove, in the
    order it drove them.
    """

    content: tuple[dict[str, Any], ...]
    model: str | None
    stop_reason: str | None
    messages: tuple[dict[str, Any], ...]
    tool_calls: tuple[SampledToolCall, ...]
    rounds: int

    @property
    def text(self) -> str:
        """Return the final answer's text blocks, joined by newlines."""
        return "\n".join(block["text"] for block in self.content if block.get("type") == "text")


def content_blocks(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a sampling result's content as a list, however it was sent.

    The spec's content field is one block or a list of them; a caller reading
    the run should not have to care which the client chose.
    """
    content = result.get("content")
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    if isinstance(content, dict):
        return [content]
    return []
