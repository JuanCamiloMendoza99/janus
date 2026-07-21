"""Loading the prompt assets that ship with the application.

The playbook is read from disk once and cached for the life of the process. It
has to be byte-identical on every request or prompt caching silently stops
working (ADR-003), and re-reading a file that is never meant to change is the
kind of thing that eventually returns something slightly different.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


@lru_cache(maxsize=1)
def load_playbook() -> str:
    """Return the support triage playbook — the cacheable prompt prefix.

    Read with an explicit UTF-8 encoding: the file contains typographic
    punctuation, and on Windows the default encoding is not UTF-8, so leaving it
    to the platform makes the bytes differ between a developer's machine and CI.
    Different bytes, different cache key.
    """
    return (_PROMPTS_DIR / "playbook.md").read_text(encoding="utf-8")
