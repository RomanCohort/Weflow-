# -*- coding: utf-8 -*-
"""Model-facing RNAHarness tool backed by the hybrid cloud/local executor."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .rna_jobs import HybridRNAExecutor, RNAToolError, TOOL_NAMES, error_payload
from .tools import Tool, ToolError, ToolSpec

RESULT_CHARS = 2500


def _bounded_json(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= RESULT_CHARS:
        return text
    compact = {
        "ok": payload.get("ok"),
        "data": {"preview": ""},
        "error": payload.get("error"),
        "provenance": payload.get("provenance"),
        "evidence": payload.get("evidence"),
        "_truncated": True,
        "_original_chars": len(text),
    }
    base = json.dumps(compact, ensure_ascii=False, default=str)
    remaining = max(0, RESULT_CHARS - len(base) - 3)
    compact["data"]["preview"] = text[:remaining] + "..."
    return json.dumps(compact, ensure_ascii=False, default=str)

_SPEC = ToolSpec(
    name="rna_cloud",
    description=(
        "Call an RNA research tool through RNAHarness. "
        "Use when you need: RNA sequence/accession identification (rna_identify, "
        "rna_fetch), miRNA/gene/taxon resolution and ID mapping (mirna_resolve, "
        "gene_resolve, id_map, taxonomy_resolve), RNA secondary/tertiary structure "
        "prediction and binding-site tools (vienna, rhofold, ncbp, gatsite, "
        "gemorna, rnadvisor), Rfam family scan (rfam_scan), literature/evidence "
        "lookup (tool_literature_search, tool_evidence_lookup, "
        "tool_citation_verify), or medical evidence (medline_search, "
        "clinical_trials_search, fda_drug_label, fda_drug_event_signal). "
        "Available tools: " + ", ".join(TOOL_NAMES) + ". "
        "Pass the exact tool name and a params object matching that tool's schema. "
        "Long jobs run end-to-end inside this tool and return one final result; "
        "do not issue submit/status/file loops yourself. An empty result includes "
        "coverage and is not evidence of absence. Never invent tool output."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tool": {"type": "string", "description": "Exact tool name from the list above."},
            "params": {
                "type": "object",
                "description": "Arguments for that tool. Use 'symbol' for gene_resolve, "
                               "'sequence' for rna_identify/rhofold/rfam_scan, "
                               "'name' for mirna_resolve/taxonomy_resolve, "
                               "'term' for medline_search, 'condition' for "
                               "clinical_trials_search, 'drug' for FDA tools, "
                               "'mirna'+'gene' for literature tools.",
            },
        },
        "required": ["tool", "params"],
    },
)


@dataclass
class RNACloudTool(Tool):
    spec = _SPEC

    def __init__(self, executor: HybridRNAExecutor | None = None) -> None:
        self.executor = executor or HybridRNAExecutor()

    def call(self, args: dict[str, Any]) -> str:
        tool = args.get("tool")
        params = args.get("params", {})
        if not isinstance(tool, str) or not tool.strip():
            raise ToolError("tool must be a non-empty string")
        if not isinstance(params, dict):
            raise ToolError("params must be an object")
        try:
            payload = self.executor.execute(tool.strip(), params)
        except RNAToolError as exc:
            payload = error_payload(exc)
        return _bounded_json(payload)


def register_rna_cloud_tools(tools) -> None:
    """Register into a GroupScopedTools registry (idempotent)."""
    if tools.get(_SPEC.name) is None:
        tools.register(RNACloudTool())
