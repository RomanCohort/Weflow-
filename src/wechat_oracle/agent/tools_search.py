# -*- coding: utf-8 -*-
"""Agent tool: web search for WeChat-Oracle.

Uses `ddgs` (deedy5/duckduckgo_search), MIT-licensed and actively maintained.
Only search results are returned (title / URL / snippet) — no page fetching,
so the model gets concise evidence it can cite, not raw HTML.

Why failover
------------
On this machine `ddgs` backend "auto" resolves to Google, which works for most
queries but intermittently raises `DDGSException: No results found` even for
valid queries. Other backends have complementary coverage (observed: a
cooperative-matrix query failed on google but succeeded on yandex). So we try a
short ordered chain and stop at the first non-empty result set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ddgs import DDGS

from .tools import Tool, ToolError, ToolSpec, truncate_for_llm

# Ordered by observed reliability on this host, not by upstream default.
BACKENDS = ("auto", "yandex", "bing", "duckduckgo")

MAX_RESULTS = 8
MAX_QUERY_CHARS = 300
RESULT_CHARS = 5000


_SEARCH_SPEC = ToolSpec(
    name="web_search",
    description=(
        "Search the general web for current, non-academic facts: news, product "
        "versions, documentation, everyday information. For RESEARCH questions "
        "about papers or scientific results, use paper_search instead — do NOT "
        "call both tools for the same question. Never repeat a search with a "
        "slightly different query once you already have usable results: tools "
        "are a means, not the goal. Move on to answering after at most one or "
        "two searches. If search fails, say you could not verify rather than "
        "inventing URLs or claims. Returns snippets only, not full pages."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query in any language. Keep it specific.",
                "maxLength": MAX_QUERY_CHARS,
            },
            "max_results": {
                "type": "integer",
                "description": "How many results to return (1-8). Defaults to 5.",
                "minimum": 1,
                "maximum": MAX_RESULTS,
            },
            "timelimit": {
                "type": "string",
                "enum": ["d", "w", "m", "y"],
                "description": "Optional recency filter: day / week / month / year.",
            },
        },
        "required": ["query"],
    },
)


def _search(query: str, max_results: int, timelimit: str | None):
    """Return (results, backend_name, last_error)."""
    last_error = None
    for backend in BACKENDS:
        try:
            kwargs = {"max_results": max_results, "backend": backend,
                      "timeout": 20}
            if timelimit:
                kwargs["timelimit"] = timelimit
            rows = DDGS().text(query, **kwargs)
            if rows:
                return rows, backend, None
            last_error = "empty results"
        except Exception as exc:
            last_error = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    return [], "", last_error


@dataclass
class WebSearchTool(Tool):
    spec = _SEARCH_SPEC

    def call(self, args: dict[str, Any]) -> str:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query must be a non-empty string")
        query = query.strip()[:MAX_QUERY_CHARS]

        max_results = args.get("max_results", 5)
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            max_results = 5
        max_results = max(1, min(max_results, MAX_RESULTS))

        timelimit = args.get("timelimit")
        if timelimit not in ("d", "w", "m", "y"):
            timelimit = None

        rows, backend, error = _search(query, max_results, timelimit)
        if not rows:
            raise ToolError(
                "web search found nothing for %r (last backend error: %s). "
                "Do not invent an answer; say you could not verify it."
                % (query, error or "unknown"))

        lines = ["Search results for %r (backend: %s):" % (query, backend)]
        for i, r in enumerate(rows[:max_results], 1):
            title = str(r.get("title") or "").strip()
            href = str(r.get("href") or "").strip()
            body = str(r.get("body") or "").strip()
            lines.append("%d. %s" % (i, title))
            if href:
                lines.append("   %s" % href)
            if body:
                lines.append("   %s" % body[:280])
        return truncate_for_llm("\n".join(lines), limit=RESULT_CHARS)


def register_search_tools(tools) -> None:
    """Register the search tool. Called once per agent run."""
    tools.register(WebSearchTool())
