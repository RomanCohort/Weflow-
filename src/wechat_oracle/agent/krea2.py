"""Krea2 image generation for explicit direct-address native agent turns.

The endpoint is a fixed loopback URL; no tool argument can change the URL,
headers, key-file path, or output directory. The API key lives in a protected
Windows file and is used only as an X-API-Key request header. One tool instance
belongs to one chat run and permits at most one job. Images are validated as
bounded PNG bytes, atomically saved under the current group's output directory,
and sent through the dispatcher's serialized attachment sender.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger

from ..config import settings
from .tools import Tool, ToolError, ToolSpec
from .tools_files import atomic_write_bytes, group_output_root


_BASE_URL = "http://127.0.0.1:18032"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"IEND\xaeB`\x82"
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DONE_STATUSES = {"completed", "complete", "done", "success", "succeeded"}
_FAILED_STATUSES = {"failed", "failure", "error", "cancelled", "canceled"}


class ImageDeliveryError(ToolError):
    """The image exists locally, but delivery has not been confirmed."""


_KREA_SPEC = ToolSpec(
    name="generate_group_image",
    description=(
        "Generate exactly one PNG from a text prompt using the fixed local Krea2 "
        "service, save it in this group's output directory, and attach it to the "
        "current group. Available only in explicit direct-chat turns. The service "
        "URL, credentials, and output path cannot be chosen by the model. "
        "If this tool is visible to you, Krea2 IS available RIGHT NOW: call it "
        "whenever the user asks for an image in a direct @ turn. NEVER claim "
        "'there is no image tool' or 'I cannot generate images' while this "
        "tool is in your tool list — that is false. If a call fails, report the "
        "actual error from the tool."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Image prompt, at most 1000 characters.",
                "maxLength": 1000,
            },
            "width": {
                "type": "integer",
                "description": "Width in pixels, 256..1024 and a multiple of 64. Defaults to 1024.",
                "minimum": 256,
                "maximum": 1024,
                "multipleOf": 64,
            },
            "height": {
                "type": "integer",
                "description": "Height in pixels, 256..1024 and a multiple of 64. Defaults to 1024.",
                "minimum": 256,
                "maximum": 1024,
                "multipleOf": 64,
            },
            "steps": {
                "type": "integer",
                "description": "Sampling steps, 1..30. Defaults to 8.",
                "minimum": 1,
                "maximum": 30,
            },
            "seed": {
                "type": "integer",
                "description": "Optional deterministic seed from 0 to 2147483647.",
                "minimum": 0,
                "maximum": 2147483647,
            },
        },
        "required": ["prompt"],
    },
)


def _bounded_int(value: Any, *, name: str, default: int, low: int, high: int, multiple: int = 1) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ToolError(f"{name} must be an integer from {low} to {high}")
    if value % multiple:
        raise ToolError(f"{name} must be a multiple of {multiple}")
    return value


def _api_key() -> str:
    """Load the deployment key without exposing it to prompts, logs, or results."""
    try:
        raw = settings.krea2_key_file.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ToolError("Krea2 key file unavailable; deployment setup required") from e
    if not raw or len(raw) > 4096 or "\n" in raw:
        raise ToolError("Krea2 key file is invalid; deployment setup required")
    return raw


def _client() -> httpx.Client:
    """Fixed loopback, no proxy env, no redirects, bounded per-request timeout."""
    timeout = max(1.0, min(settings.krea2_timeout_seconds, 300.0))
    return httpx.Client(
        base_url=_BASE_URL,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    )


def _request_json(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = client.request(method, url, **kwargs)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ToolError(f"Krea2 request failed: {type(e).__name__}") from e
    if not isinstance(payload, dict):
        raise ToolError("Krea2 returned an unexpected response shape")
    return payload


def _job_id(payload: dict[str, Any]) -> str:
    value = payload.get("id") or payload.get("job_id")
    if not isinstance(value, (str, int)):
        raise ToolError("Krea2 job response has no valid id")
    job_id = str(value)
    if not _JOB_ID_RE.fullmatch(job_id):
        raise ToolError("Krea2 job response has an invalid id")
    return job_id


def _image_bytes(client: httpx.Client, job_id: str) -> bytes:
    """Stream and cap the PNG before saving it; validate signature and IEND."""
    cap = max(1, min(settings.krea2_max_image_bytes, 20_000_000))
    data = bytearray()
    try:
        with client.stream("GET", f"/jobs/{job_id}/image") as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > cap:
                raise ToolError("Krea2 image exceeds the byte cap")
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > cap:
                    raise ToolError("Krea2 image exceeds the byte cap")
    except ToolError:
        raise
    except httpx.HTTPError as e:
        raise ToolError(f"Krea2 image download failed: {type(e).__name__}") from e
    raw = bytes(data)
    if len(raw) < 24 or not raw.startswith(_PNG_MAGIC) or not raw.endswith(_PNG_IEND):
        raise ToolError("Krea2 image is not a complete PNG")
    return raw


@dataclass
class GenerateGroupImageTool(Tool):
    spec = _KREA_SPEC
    group_id: str
    group_name: str | None
    attachment_sender: Any
    _used: bool = field(default=False, init=False, repr=False)

    def call(self, args: dict[str, Any]) -> str:
        if self._used:
            raise ToolError("Krea2 already generated one image in this chat turn")
        self._used = True
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ToolError("prompt must be a non-empty string")
        max_prompt = max(1, min(settings.krea2_max_prompt_chars, 1000))
        if len(prompt) > max_prompt:
            raise ToolError(f"prompt too long (max {max_prompt} chars)")
        width = _bounded_int(args.get("width"), name="width", default=1024, low=256, high=1024, multiple=64)
        height = _bounded_int(args.get("height"), name="height", default=1024, low=256, high=1024, multiple=64)
        max_steps = max(1, min(settings.krea2_max_steps, 30))
        steps = _bounded_int(args.get("steps"), name="steps", default=min(8, max_steps), low=1, high=max_steps)
        payload: dict[str, Any] = {
            "prompt": prompt,
            "width": width,
            "height": height,
            "steps": steps,
        }
        seed = args.get("seed")
        if seed is not None:
            payload["seed"] = _bounded_int(
                seed, name="seed", default=0, low=0, high=2_147_483_647,
            )
        key = _api_key()
        root = group_output_root(self.group_id)
        root.mkdir(parents=True, exist_ok=True)
        filename = f"krea2-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}.png"
        path = root / filename
        if path.exists() or path.is_symlink():
            raise ToolError("generated image name collision; retry once in a new turn")
        headers = {"X-API-Key": key}
        deadline = time.monotonic() + max(1.0, min(settings.krea2_timeout_seconds, 300.0))
        poll = max(0.5, min(settings.krea2_poll_seconds, 30.0))
        with _client() as client:
            created = _request_json(client, "POST", "/jobs", json=payload, headers=headers)
            job_id = _job_id(created)
            while True:
                status_payload = _request_json(client, "GET", f"/jobs/{job_id}", headers=headers)
                status = str(status_payload.get("status") or "").strip().lower()
                if status in _DONE_STATUSES:
                    break
                if status in _FAILED_STATUSES:
                    raise ToolError(f"Krea2 job failed with status {status!r}")
                if time.monotonic() >= deadline:
                    raise ToolError("Krea2 job timed out before completion")
                time.sleep(min(poll, max(0.1, deadline - time.monotonic())))
            image = _image_bytes(client, job_id)
        try:
            atomic_write_bytes(path, image)
        except FileExistsError as e:
            raise ToolError("generated image name collision; retry once in a new turn") from e
        try:
            self.attachment_sender(self.group_name, path, None)
        except Exception as e:
            logger.warning(
                "Krea2 image saved but group delivery failed: path={} error={}",
                path,
                e,
            )
            raise ImageDeliveryError(
                f"图片已生成并保留在 {path}，但群发送失败："
                f"{type(e).__name__}: {e}"
            ) from e
        return (
            f"generated and attached {filename} ({len(image)} bytes, "
            f"{width}x{height}, {steps} steps)"
        )


def register_direct_chat_krea_tool(tools: Any, *, attachment_sender: Any) -> None:
    """Register Krea2 only for explicit direct-chat Phase A runs."""
    tools.register(GenerateGroupImageTool(
        group_id=tools.group_id,
        group_name=tools.group_name,
        attachment_sender=attachment_sender,
    ))


__all__ = ["register_direct_chat_krea_tool"]
