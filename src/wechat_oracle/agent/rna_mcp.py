"""Signed AgentArts MCP client used by the RNA hybrid executor."""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any

import httpx


class MCPError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.status is not None:
            result["status"] = self.status
        if self.detail is not None:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True)
class MCPResponse:
    status: int
    content_type: str
    body: bytes
    events: list[dict[str, Any]]


def parse_sse(text: str) -> list[dict[str, Any]]:
    """Parse JSON data events from an SSE body, accepting LF or CRLF."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    events: list[dict[str, Any]] = []
    for block in normalized.split("\n\n"):
        data_lines = []
        for line in block.splitlines():
            if line.startswith("data:"):
                value = line[5:]
                data_lines.append(value[1:] if value.startswith(" ") else value)
        if not data_lines:
            continue
        payload = "\n".join(data_lines).strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            item = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MCPError(
                "INVALID_MCP_EVENT",
                "AgentArts returned an invalid MCP data event",
                detail={"offset": exc.pos},
            ) from None
        if isinstance(item, dict):
            events.append(item)
    return events


def _credentials() -> tuple[str, str]:
    ak = os.environ.get("HUAWEICLOUD_SDK_AK", "").strip()
    sk = os.environ.get("HUAWEICLOUD_SDK_SK", "").strip()
    if ak and sk:
        return ak, sk
    script = (
        "$a=[Environment]::GetEnvironmentVariable('HUAWEICLOUD_SDK_AK','User');"
        "$s=[Environment]::GetEnvironmentVariable('HUAWEICLOUD_SDK_SK','User');"
        "Write-Output ($a+'|'+$s)"
    )
    try:
        output = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise MCPError(
            "CLOUD_CREDENTIALS_UNAVAILABLE",
            "Huawei Cloud credentials are unavailable",
        ) from None
    if "|" not in output:
        raise MCPError(
            "CLOUD_CREDENTIALS_UNAVAILABLE",
            "Huawei Cloud credentials are unavailable",
        )
    ak, sk = output.split("|", 1)
    if not ak.strip() or not sk.strip():
        raise MCPError("CLOUD_CREDENTIALS_EMPTY", "Huawei Cloud credentials are empty")
    return ak.strip(), sk.strip()


class AgentArtsMCPClient:
    """Minimal JSON-RPC/streamable-HTTP client with no retry side effects."""

    def __init__(
        self,
        url: str,
        region: str = "cn-southwest-2",
        timeout: float = 120.0,
    ) -> None:
        self.url = url
        self.region = region
        self.timeout = timeout

    def _post_signed(
        self,
        payload: dict[str, Any],
        timeout: float | None = None,
    ) -> MCPResponse:
        try:
            from apig_sdk import signer
        except ImportError:
            raise MCPError("APIG_SDK_MISSING", "apig_sdk is not installed") from None
        ak, sk = _credentials()
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        request_id = str(uuid.uuid4())
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Sdk-Content-Sha256": "UNSIGNED-PAYLOAD",
            "x-hw-agentarts-session-id": request_id,
            "mcp-session-id": request_id,
            "mcp-protocol-version": "2025-03-26",
        }
        signer_client = signer.Signer(
            algorithm="V11-HMAC-SHA256",
            region_id=self.region,
        )
        signer_client.Key, signer_client.Secret = ak, sk
        request = signer.HttpRequest("POST", self.url, headers, body)
        signer_client.Sign(request)
        with httpx.Client(
            trust_env=False,
            timeout=timeout or self.timeout,
            follow_redirects=False,
        ) as client:
            response = client.request(
                request.method,
                f"{request.scheme}://{request.host}{request.uri}",
                headers=request.headers,
                content=request.body,
            )
        event_text = response.text
        events = parse_sse(event_text)
        if not events and event_text.strip():
            try:
                value = json.loads(event_text)
                events = [value] if isinstance(value, dict) else []
            except json.JSONDecodeError:
                events = []
        return MCPResponse(
            status=response.status_code,
            content_type=response.headers.get("content-type", ""),
            body=response.content,
            events=events,
        )

    def call(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None = None,
    ) -> Any:
        request = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex[:12],
            "method": method,
            "params": params,
        }
        response = self._post_signed(request, timeout=timeout)
        if response.status != 200:
            raise MCPError(
                "MCP_HTTP_ERROR",
                f"AgentArts MCP returned HTTP {response.status}",
                status=response.status,
                detail=response.body[:300].decode("utf-8", "replace"),
            )
        if not response.events:
            raise MCPError(
                "EMPTY_MCP_STREAM",
                f"AgentArts MCP returned an empty {response.content_type or 'response'} body",
                status=response.status,
                detail={
                    "bytes": len(response.body),
                    "content_type": response.content_type,
                },
            )
        event = response.events[0]
        if isinstance(event.get("error"), dict):
            error = event["error"]
            raise MCPError(
                str(error.get("code") or "MCP_JSONRPC_ERROR"),
                str(error.get("message") or "AgentArts MCP request failed"),
                status=response.status,
            )
        return event.get("result")

    def tools_list(self) -> list[dict[str, Any]]:
        result = self.call("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.call(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        if not isinstance(result, dict):
            raise MCPError(
                "INVALID_TOOL_RESPONSE",
                "AgentArts MCP returned no tool result",
            )
        content = result.get("content")
        if not isinstance(content, list):
            raise MCPError(
                "INVALID_TOOL_RESPONSE",
                "AgentArts MCP returned no content blocks",
            )
        text = "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = {"ok": True, "data": {"text": text}}
        else:
            parsed = {"ok": True, "data": result}
        if result.get("isError") and isinstance(parsed, dict):
            parsed["ok"] = False
        return parsed if isinstance(parsed, dict) else {"ok": True, "data": parsed}
