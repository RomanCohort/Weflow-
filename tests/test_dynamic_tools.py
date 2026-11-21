"""Approval-gated dynamic tool tests; no real Docker calls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_oracle.agent.dynamic_tools import (
    ToolError,
    create_proposal,
    disable_tool,
    list_approved,
    list_proposals,
    promote_proposal,
    rollback_tool,
    run_approved,
)
from wechat_oracle.config import settings


CODE = """\
def run(args):
    return {"sum": args["a"] + args["b"]}
"""
TEST = """\
import tool
assert tool.run({"a": 2, "b": 3}) == {"sum": 5}
"""
SCHEMA = {
    "type": "object",
    "properties": {
        "a": {"type": "number"},
        "b": {"type": "number"},
    },
    "required": ["a", "b"],
    "additionalProperties": False,
}


class DynamicToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.temp.name)
        self.addCleanup(setattr, settings, "data_dir", self.old_data_dir)
        self.sandbox = patch(
            "wechat_oracle.agent.dynamic_tools.sandbox.run",
            return_value="stdout:\nSELFTEST_OK\nexit code: 0\n",
        ).start()
        self.addCleanup(patch.stopall)

    def make_proposal(self, name="add_numbers"):
        message = create_proposal(
            name=name,
            description="Add two numbers",
            parameters=SCHEMA,
            code=CODE,
            selftest=TEST,
        )
        return message.splitlines()[0].split(":", 1)[1].strip()

    def test_proposal_is_saved_only_after_selftest(self):
        proposal_id = self.make_proposal()
        self.assertEqual(len(list_proposals()), 1)
        self.assertTrue(proposal_id.startswith("add_numbers-"))
        manifest = json.loads(
            (settings.data_dir / "dynamic_tools" / "proposals" /
             proposal_id / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["name"], "add_numbers")
        self.assertEqual(self.sandbox.call_count, 1)

    def test_unsafe_import_is_rejected_before_sandbox(self):
        with self.assertRaisesRegex(ToolError, "non-allowlisted"):
            create_proposal(
                name="bad_network",
                description="bad",
                parameters={"type": "object", "properties": {}},
                code="import socket\ndef run(args):\n return None\n",
                selftest="import tool\n",
            )
        self.sandbox.assert_not_called()
        self.assertEqual(list_proposals(), [])

    def test_promote_run_disable_and_rollback(self):
        proposal_id = self.make_proposal()
        manifest = promote_proposal(proposal_id)
        self.assertEqual(manifest["version"], 1)
        self.assertTrue(list_approved()[0]["enabled"])
        with patch(
            "wechat_oracle.agent.dynamic_tools.sandbox.run",
            return_value='{"tool":"add_numbers","result":{"sum":5}}\nexit code: 0',
        ) as run:
            result = run_approved("add_numbers", {"a": 2, "b": 3})
        self.assertIn('"sum":5', result)
        self.assertEqual(json.loads(run.call_args.kwargs["stdin"]), {"a": 2, "b": 3})

        second_id = self.make_proposal()
        second = promote_proposal(second_id)
        self.assertEqual(second["version"], 2)
        self.assertEqual(rollback_tool("add_numbers"), 1)
        disable_tool("add_numbers")
        self.assertFalse(list_approved()[0]["enabled"])
        with self.assertRaisesRegex(ToolError, "disabled"):
            run_approved("add_numbers", {"a": 1, "b": 1})


if __name__ == "__main__":
    unittest.main()
