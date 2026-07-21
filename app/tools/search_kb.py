"""Tool: search the support knowledge base.

Phase 2 backs this with a small in-repo corpus rather than a real search index —
the point of this project is the LLM plumbing, and the sibling Veridex project
already demonstrates retrieval properly. Keeping it simple here avoids building
a worse second RAG system.

Ranking is deterministic keyword overlap. That is a feature for the tests (the
same query always returns the same articles in the same order) and a stated
limitation everywhere else.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.providers.base import ToolSpec
from app.tools.kb import ARTICLES, Article
from app.tools.schema import json_schema_for

#: Matches on the title or the tags are worth more than a match buried in prose.
_TITLE_WEIGHT = 3
_TAG_WEIGHT = 4
_BODY_WEIGHT = 1

#: Words that match everything and therefore rank nothing. Not a real stoplist —
#: just enough that "the" does not outvote "refund".
_STOPWORDS = frozenset(
    """
    a an and are as at be been but by can cant did do does for from get got had has have
    how i im in is it its me my not of on or our so that the their them there they this
    to us was we were what when why will with you your
    """.split()
)

_EXCERPT_CHARS = 240


class SearchKbArgs(BaseModel):
    """Arguments accepted by `search_kb`.

    `extra="forbid"` on purpose: a model that invents an argument should be told
    so, not silently ignored. The correction costs one turn and it teaches the
    model the actual shape of the tool.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        description="Natural-language description of the customer's problem.",
    )
    limit: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum number of articles to return.",
    )


SPEC = ToolSpec(
    name="search_kb",
    description=(
        "Search the support knowledge base for articles relevant to a customer "
        "issue. Call this before proposing an automated reply, so the reply is "
        "grounded in a documented answer rather than a guess."
    ),
    parameters=json_schema_for(SearchKbArgs),
)


def _tokenize(text: str) -> list[str]:
    return [word for word in re.findall(r"[a-z0-9]+", text.lower()) if word not in _STOPWORDS]


def _score(article: Article, terms: set[str]) -> int:
    title_terms = set(_tokenize(article.title))
    tag_terms = {tag.lower() for tag in article.tags}
    body_terms = set(_tokenize(article.body))
    return (
        _TITLE_WEIGHT * len(terms & title_terms)
        + _TAG_WEIGHT * len(terms & tag_terms)
        + _BODY_WEIGHT * len(terms & body_terms)
    )


def _excerpt(article: Article) -> str:
    if len(article.body) <= _EXCERPT_CHARS:
        return article.body
    return article.body[:_EXCERPT_CHARS].rsplit(" ", 1)[0] + "…"


async def search_kb(query: str, limit: int = 3) -> dict[str, Any]:
    """Return knowledge base articles matching `query`, best first.

    Ties break on article id so the ordering is total and reproducible — an
    unstable ranking would make the evaluation set in Phase 4 meaningless.
    """
    terms = set(_tokenize(query))
    scored = [(article, _score(article, terms)) for article in ARTICLES]
    hits = sorted(
        (pair for pair in scored if pair[1] > 0),
        key=lambda pair: (-pair[1], pair[0].id),
    )[:limit]

    articles = [
        {
            "id": article.id,
            "title": article.title,
            "excerpt": _excerpt(article),
            "score": score,
        }
        for article, score in hits
    ]
    result: dict[str, Any] = {"query": query, "articles": articles}
    if not articles:
        # Said explicitly, because the alternative is a model that reads an empty
        # list as permission to answer from memory and present it as documented.
        result["note"] = (
            "No knowledge base article matches this query. Say so rather than "
            "inventing a documented answer."
        )
    return result
