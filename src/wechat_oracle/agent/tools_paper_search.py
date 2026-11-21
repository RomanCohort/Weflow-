# -*- coding: utf-8 -*-
"""Agent tool: academic literature search via the site's OpenSearch service.

Backend: https://www.romancohort.cloud/api/asksearch — a curated search over
arXiv / Europe PMC / PubMed / OpenAlex / Crossref / DOAJ / RCSB PDB. We do NOT
reimplement the search here: the server already does source fan-out, DOI/title
dedup, and curation. This tool is a thin, defensive client.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .tools import Tool, ToolError, ToolSpec, truncate_for_llm

API = "https://www.romancohort.cloud/api/asksearch"
SOURCES_API = "https://www.romancohort.cloud/api/sources"
TIMEOUT = 180
MAX_QUERY_CHARS = 300
MAX_RESULTS = 8
RESULT_CHARS = 5000

# Filled lazily from /api/sources; fallback matches the server's current list.
DEFAULT_SOURCES = ("arxiv", "europepmc", "pubmed", "openalex",
                   "crossref", "doaj", "pdb")
KNOWN = set(DEFAULT_SOURCES)


def _fetch_sources() -> set[str]:
    try:
        with urllib.request.urlopen(SOURCES_API, timeout=20) as r:
            d = json.load(r)
        keys = {x["key"] for x in d.get("sources", []) if isinstance(x, dict)}
        return keys or set(DEFAULT_SOURCES)
    except Exception:
        return set(DEFAULT_SOURCES)


def _search(query: str, sources: list[str]) -> dict[str, Any]:
    qs = urllib.parse.urlencode({
        "q": query,
        "sources": ",".join(sources),
        "client_id": "wechat-oracle",
    })
    req = urllib.request.Request(API + "?" + qs,
                                 headers={"User-Agent": "wechat-oracle/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except Exception as exc:
        raise ToolError("paper search failed: %s: %s"
                        % (type(exc).__name__, str(exc)[:140]))


_PAPER_SEARCH_SPEC = ToolSpec(
    name="paper_search",
    description=(
        "Search academic literature across arXiv, Europe PMC, PubMed, OpenAlex, "
        "Crossref, DOAJ and RCSB PDB. Returns a curated summary plus the top "
        "papers with titles, authors, dates and abstracts. Use this ONLY for "
        "research questions about papers, published results, or scientific "
        "facts needing a source. For news, docs or everyday facts use web_search "
        "instead — never call both for the same question. One good search is "
        "enough; do not re-query with slightly different wording once you have "
        "usable results. Tools are a means to answer, not the goal itself."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Research question or keywords, any language.",
                "maxLength": MAX_QUERY_CHARS,
            },
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional databases to search. Defaults to all. Valid keys: "
                    "arxiv, europepmc, pubmed, openalex, crossref, doaj, pdb."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RESULTS,
                "description": "How many papers to include. Defaults to 5.",
            },
        },
        "required": ["query"],
    },
)


@dataclass
class PaperSearchTool(Tool):
    spec = _PAPER_SEARCH_SPEC

    def call(self, args: dict[str, Any]) -> str:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query must be a non-empty string")
        query = query.strip()[:MAX_QUERY_CHARS]
        if len(query) < 4:
            raise ToolError("query is too short; give at least 4 characters")

        raw_sources = args.get("sources")
        if raw_sources is None:
            sources = list(DEFAULT_SOURCES)
        elif isinstance(raw_sources, list) and raw_sources:
            valid = _fetch_sources() or KNOWN
            sources = [str(s) for s in raw_sources if str(s) in valid]
            if not sources:
                raise ToolError("no valid sources; choose from: %s"
                                % ", ".join(sorted(valid)))
        else:
            sources = list(DEFAULT_SOURCES)

        max_results = args.get("max_results", 5)
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            max_results = 5
        max_results = max(1, min(max_results, MAX_RESULTS))

        data = _search(query, sources)
        if data.get("error"):
            raise ToolError("paper search error: %s" % str(data["error"])[:200])

        lines = []
        say = str(data.get("say") or "").strip()
        if say:
            lines.append("管理员精选：" + say)
        items = data.get("items") or []
        if not items:
            lines.append("本次没有找到匹配的论文。不要编造文献。")
        for i, item in enumerate(items[:max_results], 1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            authors = str(item.get("authors") or "").strip()
            date = str(item.get("date") or "").strip()
            src = str(item.get("source_label") or item.get("source") or "").strip()
            uid = str(item.get("id") or "").strip()
            abstract = str(item.get("abstract") or "").strip()
            head = "%d. [%s] %s" % (i, src or "?", title)
            if date:
                head += "  (%s)" % date
            lines.append(head)
            meta = []
            if authors:
                meta.append("作者：" + authors[:120])
            if uid:
                meta.append("ID：" + uid)
            if meta:
                lines.append("   " + " | ".join(meta))
            if abstract:
                lines.append("   " + abstract[:320])
            link = str(item.get("url") or item.get("abs_url") or "").strip()
            if link:
                lines.append("   " + link[:200])

        terms = data.get("terms") or data.get("queries") or []
        if terms:
            if isinstance(terms, list) and terms and isinstance(terms[0], dict):
                terms = [str(t.get("term") or "") for t in terms]
            lines.append("检索词：" + ", ".join(str(t) for t in terms)[:200])
        return truncate_for_llm("\n".join(lines), limit=RESULT_CHARS)


def register_paper_search_tools(tools) -> None:
    """Register the literature-search tool. Called once per agent run."""
    tools.register(PaperSearchTool())
