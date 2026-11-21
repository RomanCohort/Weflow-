import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_oracle.agent.rna_jobs import HybridRNAExecutor, TOOL_NAMES
from wechat_oracle.agent.rna_mcp import MCPError
from wechat_oracle.agent.tools_rna_cloud import RNACloudTool


class FakeMCP:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True, "data": {"tool": name}, "provenance": {"source": "cloud"}}


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def run_tool(self, name, payload, timeout_s):
        self.calls.append((name, payload, timeout_s))
        return {
            "ok": True,
            "data": {"job_id": "job-1", "structure": "((...))"},
            "provenance": {"source": "local"},
        }


class HybridTests(unittest.TestCase):
    def test_catalog_has_v11_surface(self):
        self.assertEqual(len(TOOL_NAMES), 23)
        self.assertIn("medline_search", TOOL_NAMES)
        self.assertIn("fda_drug_event_signal", TOOL_NAMES)

    def test_short_tool_routes_to_mcp(self):
        mcp = FakeMCP()
        executor = HybridRNAExecutor(mcp=mcp, harness_root=Path("."), timeout_seconds=5)
        result = executor.execute("gene_resolve", {"symbol": "TP53"})
        self.assertTrue(result["ok"])
        self.assertEqual(mcp.calls, [("gene_resolve", {"symbol": "TP53"})])

    def test_long_tool_routes_once_to_local_adapter(self):
        mcp = FakeMCP()
        adapter = FakeAdapter()
        executor = HybridRNAExecutor(mcp=mcp, harness_root=Path("."), timeout_seconds=30)
        with patch.object(executor, "_local_adapter", return_value=adapter):
            result = executor.execute(
                "vienna",
                {"tool": "fold", "params": {"sequence": "GCGAAUUCGGCGC"}},
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["provenance"]["executor"], "local_rnaharness")
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(mcp.calls, [])

    def test_lab_run_wraps_only_supported_local_tool(self):
        mcp = FakeMCP()
        adapter = FakeAdapter()
        executor = HybridRNAExecutor(mcp=mcp, harness_root=Path("."), timeout_seconds=30)
        with patch.object(executor, "_local_adapter", return_value=adapter):
            result = executor.execute(
                "lab_run",
                {"tool": "vienna", "params": {"tool": "fold", "params": {"sequence": "ACG"}}},
            )
        self.assertTrue(result["ok"])
        self.assertEqual(adapter.calls[0][0], "vienna")

    def test_uncertain_cloud_error_is_not_retried(self):
        class FailingMCP:
            def __init__(self):
                self.calls = 0

            def call_tool(self, name, arguments):
                self.calls += 1
                raise MCPError("EMPTY_MCP_STREAM", "empty")

        mcp = FailingMCP()
        executor = HybridRNAExecutor(mcp=mcp, harness_root=Path("."), timeout_seconds=5)
        result = RNACloudTool(executor=executor).call(
            {"tool": "medline_search", "params": {"term": "TP53"}}
        )
        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "EMPTY_MCP_STREAM")
        self.assertEqual(mcp.calls, 1)

    def test_large_local_result_is_compact_valid_json(self):
        class LargeAdapter:
            def run_tool(self, name, payload, timeout_s):
                return {
                    "ok": True,
                    "data": {
                        "job_id": "job-large",
                        "result": {
                            "structure": "((...))",
                            "bpp": {"pairs": [[i, i + 1] for i in range(100)]},
                            "svg": "x" * 5000,
                        },
                        "stdout": "y" * 5000,
                    },
                    "provenance": {"source": "local"},
                }

        executor = HybridRNAExecutor(
            mcp=FakeMCP(),
            harness_root=Path("."),
            timeout_seconds=5,
        )
        with patch.object(executor, "_local_adapter", return_value=LargeAdapter()):
            text = RNACloudTool(executor=executor).call(
                {"tool": "vienna", "params": {"tool": "fold", "params": {"sequence": "ACG"}}}
            )
        payload = json.loads(text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["result"]["structure"], "((...))")
        self.assertEqual(payload["data"]["result"]["bpp"]["pair_count"], 100)
        self.assertEqual(payload["data"]["result"]["svg_chars"], 5000)


if __name__ == "__main__":
    unittest.main()
