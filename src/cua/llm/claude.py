"""Anthropic adapter for the LLMClient protocol.

Choices, and why:
  - Sonnet tier: strong at tool use, cheaper than Opus, and discovery runs once per capability.
  - One tool call per turn (disable_parallel_tool_use): every action changes the page, so refs
    from the old observation may be stale. The model must look again before acting again.
  - strict tools: tool inputs are guaranteed to match the schema; discovery still validates.
  - History is append-only. Thinking blocks are echoed back unchanged, as the API requires.
  - Server-side refusal fallback is on, so a false-positive refusal does not end a run.
  - Prompt caching on the growing prefix: each turn resends the whole conversation.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Literal

import anthropic
from anthropic.types.beta import BetaContentBlockParam, BetaMessageParam, BetaToolParam

from cua.llm.base import LLMError, Reply, ToolCall, ToolResult, ToolSpec

DEFAULT_MODEL = "claude-sonnet-5-5"
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class AnthropicClient:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        effort: Effort = "medium",
        api_key: str | None = None,
        workspace_id: str | None = None,
    ):
        # Without api_key the SDK finds credentials itself (ANTHROPIC_API_KEY, an `ant` profile).
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        # Needed only when the key is not scoped to a single workspace.
        self._workspace: str | anthropic.Omit = workspace_id or anthropic.omit
        self._model = model
        self._effort = effort

    @property
    def model(self) -> str:
        return self._model

    def conversation(self, system: str, tools: Sequence[ToolSpec]) -> AnthropicConversation:
        return AnthropicConversation(self, system, tools)


class AnthropicConversation:
    def __init__(self, client: AnthropicClient, system: str, tools: Sequence[ToolSpec]):
        self._c = client
        self._system = system
        self._tools: list[BetaToolParam] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "strict": True,
            }
            for t in tools
        ]
        self._messages: list[BetaMessageParam] = []

    async def send(
        self,
        *,
        results: Sequence[ToolResult] = (),
        text: str = "",
        image_png: bytes | None = None,
    ) -> Reply:
        content: list[BetaContentBlockParam] = [
            {
                "type": "tool_result",
                "tool_use_id": r.call_id,
                "content": r.text,
                "is_error": r.is_error,
            }
            for r in results
        ]
        if image_png:
            data = base64.standard_b64encode(image_png).decode("ascii")
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": data},
                }
            )
        if text:
            content.append({"type": "text", "text": text})
        self._messages.append({"role": "user", "content": content})

        try:
            response = await self._c._client.beta.messages.create(
                model=self._c.model,
                max_tokens=16_000,
                system=self._system,
                messages=self._messages,
                tools=self._tools,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                output_config={"effort": self._c._effort},
                cache_control={"type": "ephemeral"},
                betas=[_FALLBACK_BETA],
                fallbacks="default",
                workspace_id=self._c._workspace,
            )
        except TypeError as e:
            # The SDK only finds out at request time that no credentials are configured.
            if "authentication" not in str(e):
                raise
            raise LLMError("no Anthropic credentials: set ANTHROPIC_API_KEY") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"cannot reach the Anthropic API: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e

        # Append the full content, thinking blocks included, exactly as returned.
        self._messages.append({"role": "assistant", "content": response.content})
        reason = response.stop_reason
        stop = reason if reason in ("tool_use", "end_turn", "max_tokens", "refusal") else "other"
        return Reply(
            text="".join(b.text for b in response.content if b.type == "text"),
            calls=[
                ToolCall(id=b.id, name=b.name, input=dict(b.input))
                for b in response.content
                if b.type == "tool_use"
            ],
            stop=stop,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_input_tokens": response.usage.cache_read_input_tokens or 0,
            },
        )
