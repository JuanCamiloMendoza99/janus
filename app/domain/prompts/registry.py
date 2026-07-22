"""The prompt registry: the triage playbook as a versioned, swappable dependency.

Selecting a prompt is a configuration change, exactly like selecting a provider
(ADR-006). `TRIAGE_PROMPT` names a variant, the registry returns its text, and
nothing above it knows there is more than one — the same seam the project applies
to vendors, applied to the prompt. See ADR-009.

Three constraints govern every variant, and all three fail silently when broken:

1. **Byte-stable across requests.** Never template anything into a variant — no
   timestamp, no ticket id, no customer name. Any variation invalidates the cache
   for everything that follows it in the prompt. The ticket goes in the user turn;
   these files never mention a specific one. Variants are loaded **as-is**.

2. **Above the caching floor.** Anthropic silently declines to cache prefixes
   below a per-model floor (~4096 tokens on the Opus 4.x family and Haiku 4.5,
   ~2048 on Sonnet 4.6 and Fable 5). Below it the API accepts the `cache_control`
   marker and caches nothing: no error, no warning, `cache_creation_input_tokens`
   just comes back 0. `/v1/triage` sends no tools, so a variant is the WHOLE
   prefix on that path — it cannot borrow tokens from the tool schemas the way
   `/v1/chat` does, and has to clear 4096 on its own merits. This is a real
   constraint on how terse a "terse" variant can get, and it is why
   `measured_tokens` is recorded here and asserted live: a trim that drops a
   variant under the floor has to fail a test rather than a bill. See ADR-003.

3. **No meta-commentary in the bytes.** A variant's hypothesis, its token count
   and its provenance live in this module, never in the prompt file. Notes inside
   the cached prefix inflate one variant's token count and make the cost
   comparison unfair — variants have to compete on equal token footing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

_VARIANTS_DIR = Path(__file__).parent / "playbook"


class UnknownPromptError(LookupError):
    """The configured `TRIAGE_PROMPT` names no variant that exists.

    Raised at load time rather than falling back to the default. A typo in an
    environment variable that silently serves a different prompt is a bad day:
    every metric moves and nothing says why.
    """


@dataclass(frozen=True)
class PromptVariant:
    """One playbook variant and what is known about it.

    Attributes:
        name: The value of `TRIAGE_PROMPT` that selects it.
        hypothesis: What this variant is testing, in one line. Metadata, not
            prompt bytes — see the module docstring.
        filename: The file under `playbook/`.
        measured_tokens: Prompt tokens for a `/v1/triage` request built from this
            variant — system blocks plus one short ticket — counted with
            `messages.count_tokens` rather than estimated from characters. It
            excludes the ~1,190 tokens of `TriageResult` JSON schema that the SDK
            renders ahead of the messages on the structured-output path, because
            that part is identical for every variant and would flatter them all
            equally. Dated for the same reason prices are (ADR-005): a number
            nobody can date is a number nobody can trust.
        measured_on: When that count was last verified against the API.
    """

    name: str
    hypothesis: str
    filename: str
    measured_tokens: int
    measured_on: date


#: Every variant the registry can serve. Adding one here and dropping the file in
#: `playbook/` is the whole procedure — no other module changes.
VARIANTS: dict[str, PromptVariant] = {
    "v1-baseline": PromptVariant(
        name="v1-baseline",
        hypothesis="The playbook Phase 3 shipped, as the control.",
        filename="v1-baseline.md",
        measured_tokens=6531,
        measured_on=date(2026, 7, 22),
    ),
    "v2-examples": PromptVariant(
        name="v2-examples",
        hypothesis=("More worked boundary examples raise severity accuracy, at a token cost."),
        filename="v2-examples.md",
        measured_tokens=7913,
        measured_on=date(2026, 7, 22),
    ),
    "v3-terse": PromptVariant(
        name="v3-terse",
        hypothesis="A tighter rubric holds accuracy while cutting tokens per ticket.",
        filename="v3-terse.md",
        measured_tokens=4738,
        measured_on=date(2026, 7, 22),
    ),
}

#: What ships when `TRIAGE_PROMPT` says nothing: the champion the evidence chose.
#: `v2-examples` won a held-out A/B on a statistical tie for accuracy by having the
#: lowest dropped-ticket rate and the best free-text quality, accepting +21% prefix
#: tokens (~+11% per cached ticket). See `docs/evals/prompts.md` for the full
#: comparison and the trade-off it accepts.
DEFAULT_VARIANT = "v2-examples"


def get_variant(name: str | None = None) -> PromptVariant:
    """Return the named variant, or the default when `name` is `None`."""
    key = name or DEFAULT_VARIANT
    try:
        return VARIANTS[key]
    except KeyError:
        known = ", ".join(sorted(VARIANTS))
        raise UnknownPromptError(
            f"TRIAGE_PROMPT={key!r} is not a variant. Known: {known}."
        ) from None


@lru_cache(maxsize=len(VARIANTS))
def load_playbook(name: str | None = None) -> str:
    """Return a variant's text — the cacheable prompt prefix.

    Cached per name for the life of the process: the text has to be
    byte-identical on every request or prompt caching silently stops working, and
    re-reading a file that is never meant to change is the kind of thing that
    eventually returns something slightly different.

    Read with an explicit UTF-8 encoding: the files contain typographic
    punctuation, and on Windows the default encoding is not UTF-8, so leaving it
    to the platform makes the bytes differ between a developer's machine and CI.
    Different bytes, different cache key.
    """
    return (_VARIANTS_DIR / get_variant(name).filename).read_text(encoding="utf-8")
