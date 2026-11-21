"""Hybrid execution policy for short cloud tools and long local compute jobs."""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import settings
from .rna_mcp import AgentArtsMCPClient, MCPError


LONG_TOOLS = frozenset({
    "vienna",
    "rhofold",
    "ncbp",
    "gatsite",
    "gemorna",
    "rnadvisor",
})

SHORT_TOOLS = frozenset({
    "lab_status",
    "lab_files",
    "rna_identify",
    "rna_fetch",
    "mirna_resolve",
    "gene_resolve",
    "id_map",
    "taxonomy_resolve",
    "rfam_scan",
    "tool_literature_search",
    "tool_evidence_lookup",
    "tool_citation_verify",
    "medline_search",
    "clinical_trials_search",
    "fda_drug_label",
    "fda_drug_event_signal",
})

TOOL_NAMES = tuple(sorted(LONG_TOOLS | SHORT_TOOLS | {"lab_run"}))


class RNAToolError(RuntimeError):
    def __init__(self, code: str, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.detail is not None:
            result["detail"] = self.detail
        return result


@dataclass
class HybridRNAExecutor:
    mcp: Any | None = None
    harness_root: Path | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.mcp is None:
            self.mcp = AgentArtsMCPClient(settings.rna_mcp_url)
        if self.harness_root is None:
            self.harness_root = settings.rna_harness_root
        if self.timeout_seconds is None:
            self.timeout_seconds = settings.rna_job_timeout_seconds

    def execute(self, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        name = tool.strip()
        if not isinstance(params, dict):
            raise RNAToolError("INVALID_PARAMS", "params must be an object")
        if name in LONG_TOOLS:
            return self._run_local(name, params)
        if name == "lab_run":
            inner_tool = params.get("tool")
            inner_params = params.get("params")
            if inner_tool not in LONG_TOOLS or not isinstance(inner_params, dict):
                raise RNAToolError(
                    "LOCAL_LAB_RUN_REQUIRED",
                    "lab_run is supported only for a local long-compute tool",
                    detail={"long_tools": sorted(LONG_TOOLS)},
                )
            return self._run_local(str(inner_tool), inner_params)
        if name in SHORT_TOOLS:
            return self._run_cloud(name, params)
        raise RNAToolError(
            "UNKNOWN_TOOL",
            f"Unknown RNAHarness tool: {name}",
            detail={"available_tools": list(TOOL_NAMES)},
        )

    def _run_cloud(self, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.mcp.call_tool(tool, params)
        except MCPError as exc:
            raise RNAToolError(
                exc.code,
                str(exc),
                detail=exc.as_dict().get("detail"),
            ) from None

    def _local_adapter(self):
        root = Path(self.harness_root or "").resolve()
        if not root.is_dir():
            raise RNAToolError(
                "LOCAL_RNAHARNESS_UNAVAILABLE",
                f"RNAHarness root is unavailable: {root}",
            )
        root_text = str(root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        try:
            return importlib.import_module("rnaharness.proto.adapter")
        except ImportError as exc:
            raise RNAToolError(
                "LOCAL_RNAHARNESS_IMPORT_FAILED",
                f"RNAHarness adapter import failed: {type(exc).__name__}",
            ) from None

    def _run_local(self, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        adapter = self._local_adapter()
        try:
            result = adapter.run_tool(
                tool,
                params,
                timeout_s=float(self.timeout_seconds or 600),
            )
        except Exception as exc:
            raise RNAToolError(
                "LOCAL_JOB_ERROR",
                f"Local RNAHarness job failed: {type(exc).__name__}: {exc}",
            ) from None
        if not isinstance(result, dict):
            raise RNAToolError(
                "LOCAL_BAD_RESULT",
                "Local RNAHarness returned a non-object result",
            )
        result = self._compact_local_result(result)
        result.setdefault("provenance", {})
        if isinstance(result["provenance"], dict):
            result["provenance"]["executor"] = "local_rnaharness"
        return result

    @staticmethod
    def _compact_local_result(result: dict[str, Any]) -> dict[str, Any]:
        """Keep model-relevant fields; the worker can reproduce bulky details."""
        compact = dict(result)
        data = compact.get("data")
        if not isinstance(data, dict):
            return compact
        data = dict(data)
        inner = data.get("result")
        if isinstance(inner, dict):
            inner = dict(inner)
            svg = inner.pop("svg", None)
            if isinstance(svg, str):
                inner["svg_chars"] = len(svg)
            bpp = inner.get("bpp")
            if isinstance(bpp, dict):
                pairs = bpp.get("pairs")
                if isinstance(pairs, list):
                    inner["bpp"] = {
                        "pair_count": len(pairs),
                        "pairs_preview": pairs[:20],
                    }
            data["result"] = inner
        stdout = data.get("stdout")
        if isinstance(stdout, str) and len(stdout) > 1000:
            data["stdout"] = stdout[:1000] + "\n[truncated]"
        compact["data"] = data
        return compact


def error_payload(exc: RNAToolError) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "error": exc.as_dict(),
        "provenance": None,
        "evidence": None,
    }
