"""The prompt assets that ship with the application.

The playbook is not one file any more: it is a set of versioned variants behind
`registry.py`, selected by `TRIAGE_PROMPT`. Everything a caller needs is
re-exported here, so the import stays `from app.domain.prompts import
load_playbook` whether or not the caller cares which variant it gets.
"""

from app.domain.prompts.registry import (
    DEFAULT_VARIANT,
    VARIANTS,
    PromptVariant,
    UnknownPromptError,
    get_variant,
    load_playbook,
)

__all__ = [
    "DEFAULT_VARIANT",
    "VARIANTS",
    "PromptVariant",
    "UnknownPromptError",
    "get_variant",
    "load_playbook",
]
