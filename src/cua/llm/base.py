"""The LLM seam. Discovery talks to this protocol; adapters translate to a vendor's API.

Kept vendor-neutral and small: a conversation with tools, where each turn the model gets the
results of its last tool calls plus a fresh observation, and answers with text and tool calls.
The adapter owns the message history, so it can keep it in whatever shape its API wants
(Anthropic needs thinking blocks echoed back unchanged, for example).

Replay never imports this package; tests/test_boundaries.py enforces that.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    input: dict[str, Any]


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str
    text: str
    is_error: bool = False


class Reply(BaseModel):
    text: str = ""
    calls: list[ToolCall] = []
    stop: Literal["tool_use", "end_turn", "max_tokens", "refusal", "other"]
    usage: dict[str, int] = Field(default_factory=dict)


class Conversation(Protocol):
    async def send(
        self,
        *,
        results: Sequence[ToolResult] = (),
        text: str = "",
        image_png: bytes | None = None,
    ) -> Reply:
        """Send tool results for the previous turn, then text and an optional screenshot."""
        ...


class LLMClient(Protocol):
    @property
    def model(self) -> str: ...

    def conversation(self, system: str, tools: Sequence[ToolSpec]) -> Conversation: ...


class LLMError(Exception):
    """The model could not be reached or answered with something unusable. A hard failure."""
