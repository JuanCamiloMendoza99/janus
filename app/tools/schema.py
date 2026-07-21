"""Deriving a tool's JSON Schema from its Pydantic argument model.

A tool has two obligations that must agree: it publishes a JSON Schema so the
model knows how to call it, and it validates whatever the model actually sent
before the handler runs. Written separately they drift — the schema says
`severity` is an enum, the handler quietly accepts anything — and the drift is
invisible until a live model finds it.

So the model class is the single source of truth and the schema is derived from
it. `parameters` in the `ToolSpec` and the validator in `dispatch()` are then
the same statement, by construction.

Both vendors accept plain JSON Schema, so no vendor-specific shaping happens
here. The only edit is dropping Pydantic's auto-generated `title` keys: they are
noise no model needs, they cost tokens on every request, and tool definitions
are rendered inside the cached prefix where size matters (ADR-003).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def _strip_titles(node: Any) -> Any:
    """Recursively remove `title` keys from a generated schema."""
    if isinstance(node, dict):
        return {key: _strip_titles(value) for key, value in node.items() if key != "title"}
    if isinstance(node, list):
        return [_strip_titles(item) for item in node]
    return node


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Return the JSON Schema a provider should advertise for `model`."""
    return _strip_titles(model.model_json_schema())
