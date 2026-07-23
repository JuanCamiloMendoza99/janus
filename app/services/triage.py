"""Prompt assembly for `POST /v1/triage`.

The whole of this module is one decision applied twice: what is stable goes in
the cacheable prefix, what varies goes in the user turn. Get it backwards — put
the ticket id in the prefix "just to give the model context" — and the cache
hit rate silently drops to zero while every test still passes. There is no
error for that, which is why `tests/test_triage.py` asserts that two different
tickets produce a byte-identical prefix.
"""

from __future__ import annotations

from app.api.schemas import TriageRequest
from app.domain.triage import TriageResult
from app.providers.base import LLMProvider, Message, ParsedCompletion, Prompt


def render_ticket(request: TriageRequest) -> str:
    """Render a ticket as the single user turn.

    Tagged rather than free-form so the model can tell the subject from the body
    without guessing — the playbook's PII rule turns on exactly that boundary
    (what the customer pasted into the body, versus the envelope the ticket
    arrived in). The fields are interpolated verbatim: this is the volatile part
    of the prompt and it is *supposed* to change per request.
    """
    return (
        f'<ticket id="{request.ticket_id}">\n'
        f"<subject>{request.subject}</subject>\n"
        f"<body>\n{request.body}\n</body>\n"
        f"</ticket>"
    )


def build_triage_prompt(request: TriageRequest, playbook: str) -> Prompt:
    """Assemble the prompt for one ticket.

    `cacheable_prefix` is the playbook and nothing else. The text arrives as an
    argument rather than being fetched here: which variant is in play is a
    configuration decision (ADR-009), and a service that read the setting itself
    could not be swept over the prompt axis in one process — which is exactly
    what `app/evals/runner.py` does.

    `system` stays `None`. There is nothing per-request to say that is not
    already in the ticket, and anything put there would render *after* the
    prefix — safe for the cache, but a second place to look for instructions
    that already live in the playbook.
    """
    return Prompt(
        cacheable_prefix=playbook,
        system=None,
        messages=[Message(role="user", content=render_ticket(request))],
    )


async def triage_ticket(
    provider: LLMProvider,
    request: TriageRequest,
    playbook: str,
) -> ParsedCompletion[TriageResult]:
    """Classify one ticket into a validated `TriageResult`.

    Not streamed and not run through the tool loop. The consumer of a structured
    verdict is another system, and a single constrained call is the whole
    interaction: giving the model tools here would mean a `tool_use` turn that
    `parse()` has no way to answer.
    """
    return await provider.parse(build_triage_prompt(request, playbook), TriageResult)
