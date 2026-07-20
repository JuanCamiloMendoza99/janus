"""Tool: search the support knowledge base.

Phase 2 backs this with a small in-repo corpus rather than a real search index —
the point of this project is the LLM plumbing, and the sibling Veridex project
already demonstrates retrieval properly. Keeping it simple here avoids building
a worse second RAG system.
"""

from __future__ import annotations

from typing import Any

from app.providers.base import ToolSpec

SPEC = ToolSpec(
    name="search_kb",
    description=(
        "Search the support knowledge base for articles relevant to a customer "
        "issue. Call this before proposing an automated reply, so the reply is "
        "grounded in a documented answer rather than a guess."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language description of the customer's problem.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of articles to return.",
                "default": 3,
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)


async def search_kb(query: str, limit: int = 3) -> dict[str, Any]:
    """Return knowledge base articles matching `query`."""
    raise NotImplementedError("Phase 2")
