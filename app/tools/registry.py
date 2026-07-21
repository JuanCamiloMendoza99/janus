"""Tool registry and dispatch.

Tools are declared once in vendor-neutral `ToolSpec` form and handed to whichever
provider is active. Neither this module nor the tool implementations know which
vendor will end up invoking them — the adapters own that translation.

Tool order is fixed and stable because tool definitions are rendered *first* in
the prompt, ahead of the system block. Reordering them changes the cached prefix
and invalidates the cache for everything after it. Hence a tuple, not a dict
built by iteration. See ADR-003.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.logging import get_logger
from app.providers.base import ToolResult, ToolSpec
from app.tools import escalate_ticket as escalate_ticket_tool
from app.tools import search_kb as search_kb_tool

logger = get_logger("janus.tools")

TOOL_SPECS: tuple[ToolSpec, ...] = (
    search_kb_tool.SPEC,
    escalate_ticket_tool.SPEC,
)

#: Handler plus the model that validates its arguments. Paired here so a tool
#: cannot be registered with a validator that belongs to a different tool.
_Handler = Callable[..., Awaitable[dict[str, Any]]]

_TOOLS: dict[str, tuple[_Handler, type[BaseModel]]] = {
    search_kb_tool.SPEC.name: (search_kb_tool.search_kb, search_kb_tool.SearchKbArgs),
    escalate_ticket_tool.SPEC.name: (
        escalate_ticket_tool.escalate_ticket,
        escalate_ticket_tool.EscalateTicketArgs,
    ),
}


def get_tool_specs() -> Sequence[ToolSpec]:
    """Return every registered tool, in stable order."""
    return TOOL_SPECS


def _format_validation_error(exc: ValidationError) -> str:
    """Render a Pydantic error as one line the model can act on.

    Deliberately terse and field-oriented. The model does not need a stack
    trace; it needs to know which argument was wrong and why, in the few tokens
    it will spend re-reading its own mistake.
    """
    problems = [
        f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
        for error in exc.errors()
    ]
    return "; ".join(problems)


async def dispatch(name: str, arguments: dict[str, Any], call_id: str) -> ToolResult:
    """Execute a tool call and wrap the outcome as a `ToolResult`.

    Never raises for a tool-level failure. A raised exception would leave the
    call unpaired, and both vendors reject a follow-up request whose tool_use
    block has no matching result. Failures come back as
    `ToolResult(is_error=True)` with a readable message the model can act on.

    Three things can go wrong and all three are recoverable in-conversation: the
    tool does not exist, the arguments do not fit its schema, or the handler
    itself blows up. Only `Exception` is caught — `asyncio.CancelledError` is a
    `BaseException` and must keep propagating, or a client disconnecting
    mid-stream would hang the request instead of tearing it down.
    """
    entry = _TOOLS.get(name)
    if entry is None:
        known = ", ".join(spec.name for spec in TOOL_SPECS)
        logger.warning("tool.unknown", extra={"tool": name, "call_id": call_id})
        return ToolResult(
            call_id=call_id,
            content=f"Unknown tool {name!r}. Available tools: {known}.",
            is_error=True,
        )

    handler, args_model = entry

    try:
        args = args_model.model_validate(arguments)
    except ValidationError as exc:
        logger.warning("tool.invalid_arguments", extra={"tool": name, "call_id": call_id})
        return ToolResult(
            call_id=call_id,
            content=f"Invalid arguments for {name}: {_format_validation_error(exc)}",
            is_error=True,
        )

    try:
        result = await handler(**args.model_dump())
    except Exception as exc:  # noqa: BLE001 - a tool failure is data, not a crash
        logger.warning(
            "tool.failed",
            extra={"tool": name, "call_id": call_id, "error": str(exc)},
            exc_info=True,
        )
        return ToolResult(
            call_id=call_id,
            content=f"Tool {name} failed: {type(exc).__name__}: {exc}",
            is_error=True,
        )

    return ToolResult(call_id=call_id, content=json.dumps(result, default=str))
