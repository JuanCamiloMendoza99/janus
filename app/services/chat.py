"""Prompt assembly for `POST /v1/chat`.

The router's job ends at HTTP. Deciding what the model is told, and which tools
it may reach for, is business logic and lives here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from app.api.schemas import ChatMessage
from app.providers.base import LLMProvider, Message, Prompt, StreamEvent
from app.services.tool_loop import run_tool_loop
from app.tools.registry import get_tool_specs


def build_chat_prompt(messages: Sequence[ChatMessage], playbook: str) -> Prompt:
    """Turn a client's conversation into a `Prompt`.

    The playbook goes in `cacheable_prefix`, never in `system`: it is the large,
    byte-stable text that a burst of requests should pay for once, and putting
    anything volatile ahead of it would invalidate the cache for everything that
    follows (ADR-003). `system` stays empty precisely because there is nothing
    per-request to say — the moment there is, it goes there and not in the
    prefix.

    Which variant the text came from is the caller's decision (ADR-009). Chat
    gets the same one triage does: there is one playbook, and serving chat a
    variant nobody measured would make `/v1/chat` the untested half of the
    prompt.
    """
    return Prompt(
        cacheable_prefix=playbook,
        system=None,
        messages=[Message(role=message.role, content=message.content) for message in messages],
    )


def stream_chat(
    provider: LLMProvider,
    messages: Sequence[ChatMessage],
    use_tools: bool,
    max_iterations: int,
    playbook: str,
) -> AsyncIterator[StreamEvent]:
    """Stream one chat exchange, with the tool loop if tools are enabled.

    `use_tools=False` still goes through the loop. With no tools to call the
    model cannot ask for one, so the loop runs exactly one iteration and passes
    the stream straight through — one code path instead of two that have to be
    kept in agreement.
    """
    return run_tool_loop(
        provider=provider,
        prompt=build_chat_prompt(messages, playbook),
        tools=get_tool_specs() if use_tools else (),
        max_iterations=max_iterations,
    )
