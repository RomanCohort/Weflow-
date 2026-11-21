import json
import unittest
from unittest.mock import patch

from wechat_oracle.agent.rna_mcp import (
    AgentArtsMCPClient,
    MCPError,
    MCPResponse,
    parse_sse,
)


class SSETests(unittest.TestCase):
    def test_lf_and_crlf_events(self):
        lf = 'data: {"jsonrpc":"2.0","id":"1","result":{"ok":true}}\n\n'
        crlf = 'data: {"jsonrpc":"2.0",\r\ndata: "id":"2","result":{"ok":true}}\r\n\r\n'
        self.assertEqual(parse_sse(lf)[0]["id"], "1")
        self.assertEqual(parse_sse(crlf)[0]["id"], "2")


class ClientTests(unittest.TestCase):
    def test_tool_call_parses_json_text_block(self):
        client = AgentArtsMCPClient("https://example.invalid/runtime")
        event = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"ok": True, "data": {"id": "HGNC:11998"}}),
                    }
                ],
                "isError": False,
            },
        }
        body = ('data: ' + json.dumps(event) + '\n\n').encode()
        response = MCPResponse(200, "text/event-stream", body, [event])
        with patch.object(client, "_post_signed", return_value=response):
            result = client.call_tool("gene_resolve", {"symbol": "TP53"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["id"], "HGNC:11998")

    def test_empty_stream_is_structured_error(self):
        client = AgentArtsMCPClient("https://example.invalid/runtime")
        response = MCPResponse(200, "text/event-stream", b"", [])
        with patch.object(client, "_post_signed", return_value=response):
            with self.assertRaises(MCPError) as caught:
                client.call("tools/call", {"name": "vienna", "arguments": {}})
        self.assertEqual(caught.exception.code, "EMPTY_MCP_STREAM")


if __name__ == "__main__":
    unittest.main()
