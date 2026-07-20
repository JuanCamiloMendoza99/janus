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

from collections.abc import Sequence
from typing import Any

from app.providers.base import ToolResult, ToolSpec
from app.tools import escalate_ticket as escalate_ticket_tool
from app.tools import search_kb as search_kb_tool

TOOL_SPECS: tuple[ToolSpec, ...] = (
    search_kb_tool.SPEC,
    escalate_ticket_tool.SPEC,
)

_HANDLERS = {
    search_kb_tool.SPEC.name: search_kb_tool.search_kb,
    escalate_ticket_tool.SPEC.name: escalate_ticket_tool.escalate_ticket,
}


class UnknownToolError(LookupError):
    """The model asked for a tool that is not registered."""


def get_tool_specs() -> Sequence[ToolSpec]:
    """Return every registered tool, in stable order."""
    return TOOL_SPECS


async def dispatch(name: str, arguments: dict[str, Any], call_id: str) -> ToolResult:
    """Execute a tool call and wrap the outcome as a `ToolResult`.

    Never raises for a tool-level failure. A raised exception would leave the
    call unpaired, and both vendors reject a follow-up request whose tool_use
    block has no matching result. Failures come back as
    `ToolResult(is_error=True)` with a readable message the model can act on.
    """
    raise NotImplementedError("Phase 2")
