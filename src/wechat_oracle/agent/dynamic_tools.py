"""Sandboxed, approval-gated tools authored by the agent.

Proposals are inert files under ``data/dynamic_tools``. Only the local CLI can
promote one. Approved tools execute in the existing throwaway Docker sandbox,
never in the agent process.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import sandbox
from ..config import settings
from .tools import Tool, ToolError, ToolSpec, truncate_for_llm

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
_PROPOSAL_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}-\d{14}-[a-f0-9]{6}$")
_MAX_CODE_CHARS = 80_000
_MAX_TEST_CHARS = 40_000
_MAX_OUTPUT_CHARS = 4_000
_SAFE_IMPORTS = {
    "collections",
    "datetime",
    "decimal",
    "fractions",
    "functools",
    "itertools",
    "json",
    "math",
    "random",
    "re",
    "statistics",
    "string",
    "textwrap",
    "time",
    "typing",
    "unicodedata",
    "uuid",
}
_FORBIDDEN_CALLS = {
    "__import__",
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "globals",
    "input",
    "locals",
    "open",
    "setattr",
    "vars",
}


def _root() -> Path:
    return settings.data_dir / "dynamic_tools"


def _proposal_root() -> Path:
    return _root() / "proposals"


def _approved_root() -> Path:
    return _root() / "approved"


def _safe_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME_RE.fullmatch(name):
        raise ToolError("tool name must match [a-z][a-z0-9_]{2,40}")
    return name


def _safe_proposal_id(proposal_id: str) -> str:
    proposal_id = (proposal_id or "").strip()
    if not _PROPOSAL_RE.fullmatch(proposal_id):
        raise ToolError("invalid proposal id")
    return proposal_id


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except Exception:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ToolError(f"invalid JSON file: {path.name}") from exc
    if not isinstance(value, dict):
        raise ToolError(f"invalid JSON object: {path.name}")
    return value


def _validate_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise ToolError("parameters must be a JSON-schema object")
    if schema.get("type") != "object":
        raise ToolError("parameters.type must be 'object'")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ToolError("parameters.properties must be an object")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
        raise ToolError("parameters.required must be a list of strings")
    if any(name not in properties for name in required):
        raise ToolError("parameters.required references an unknown property")
    normalized = dict(schema)
    normalized.setdefault("additionalProperties", False)
    if normalized["additionalProperties"] is not False:
        raise ToolError("additionalProperties must be false")
    if len(json.dumps(normalized, ensure_ascii=False)) > 20_000:
        raise ToolError("parameters schema is too large")
    return normalized


def _validate_python(source: str, *, label: str, require_run: bool) -> None:
    if not isinstance(source, str) or not source.strip():
        raise ToolError(f"{label} must be non-empty Python source")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ToolError(f"{label} has a syntax error: {exc.msg}") from exc
    run_defs = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for imported in names:
                root = imported.split(".", 1)[0]
                if root not in _SAFE_IMPORTS and not (label == "selftest" and root == "tool"):
                    raise ToolError(f"{label} imports non-allowlisted module: {root}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _FORBIDDEN_CALLS:
                raise ToolError(f"{label} uses forbidden call: {node.func.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ToolError(f"{label} accesses forbidden attribute: {node.attr}")
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            run_defs += 1
            if len(node.args.args) != 1:
                raise ToolError("run(args) must accept exactly one argument")
    if require_run and run_defs != 1:
        raise ToolError("tool source must define exactly one run(args) function")


def _run_selftest(code: str, selftest: str, *, timeout_s: int = 15) -> str:
    harness = (
        "import runpy\n"
        "runpy.run_path('selftest.py', run_name='__main__')\n"
        "print('SELFTEST_OK')\n"
    )
    result = sandbox.run(
        "python",
        harness,
        extra_files={"tool.py": code, "selftest.py": selftest},
        timeout_s=timeout_s,
    )
    if "SELFTEST_OK" not in result or "exit code: 0" not in result:
        raise ToolError("generated tool selftest failed:\n" + result)
    return result


def create_proposal(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    code: str,
    selftest: str,
) -> str:
    name = _safe_name(name)
    description = (description or "").strip()
    if not description or len(description) > 500:
        raise ToolError("description must be 1-500 characters")
    schema = _validate_schema(parameters)
    if len(code) > _MAX_CODE_CHARS:
        raise ToolError("tool source is too large")
    if len(selftest) > _MAX_TEST_CHARS:
        raise ToolError("selftest source is too large")
    _validate_python(code, label="tool", require_run=True)
    _validate_python(selftest, label="selftest", require_run=False)
    test_result = _run_selftest(code, selftest)

    proposal_id = f"{name}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    destination = _proposal_root() / proposal_id
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "tool.py").write_text(code, encoding="utf-8", newline="\n")
    (destination / "selftest.py").write_text(selftest, encoding="utf-8", newline="\n")
    (destination / "test_result.txt").write_text(
        test_result, encoding="utf-8", newline="\n"
    )
    manifest = {
        "proposal_id": proposal_id,
        "name": name,
        "description": description,
        "parameters": schema,
        "created_at": int(time.time()),
        "sha256": _sha256(code.encode("utf-8")),
        "selftest_sha256": _sha256(selftest.encode("utf-8")),
    }
    _write_json(destination / "manifest.json", manifest)
    return (
        f"proposal created: {proposal_id}\n"
        f"review with: wechat-oracle tool show {proposal_id}\n"
        f"approve with: wechat-oracle tool promote {proposal_id}"
    )


def load_proposal(proposal_id: str) -> dict[str, Any]:
    proposal_id = _safe_proposal_id(proposal_id)
    directory = _proposal_root() / proposal_id
    manifest = _read_json(directory / "manifest.json")
    if manifest.get("proposal_id") != proposal_id:
        raise ToolError("proposal manifest id mismatch")
    code = (directory / "tool.py").read_text(encoding="utf-8")
    selftest = (directory / "selftest.py").read_text(encoding="utf-8")
    if manifest.get("sha256") != _sha256(code.encode("utf-8")):
        raise ToolError("proposal source hash mismatch")
    if manifest.get("selftest_sha256") != _sha256(selftest.encode("utf-8")):
        raise ToolError("proposal selftest hash mismatch")
    return {**manifest, "code": code, "selftest": selftest}


def list_proposals() -> list[dict[str, Any]]:
    root = _proposal_root()
    if not root.is_dir():
        return []
    rows = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or not _PROPOSAL_RE.fullmatch(path.name):
            continue
        try:
            rows.append(_read_json(path / "manifest.json"))
        except ToolError:
            continue
    return rows


def promote_proposal(proposal_id: str) -> dict[str, Any]:
    proposal = load_proposal(proposal_id)
    _validate_python(proposal["code"], label="tool", require_run=True)
    _validate_python(proposal["selftest"], label="selftest", require_run=False)
    _run_selftest(proposal["code"], proposal["selftest"])

    name = _safe_name(proposal["name"])
    approved = _approved_root() / name
    approved.mkdir(parents=True, exist_ok=True)
    state_path = approved / "state.json"
    state = _read_json(state_path) if state_path.is_file() else {}
    version = int(state.get("current_version", 0)) + 1
    directory = approved / f"v{version}"
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "tool.py").write_text(
        proposal["code"], encoding="utf-8", newline="\n"
    )
    (directory / "selftest.py").write_text(
        proposal["selftest"], encoding="utf-8", newline="\n"
    )
    manifest = {
        "proposal_id": proposal_id,
        "name": name,
        "description": proposal["description"],
        "parameters": proposal["parameters"],
        "version": version,
        "approved_at": int(time.time()),
        "sha256": proposal["sha256"],
        "selftest_sha256": proposal["selftest_sha256"],
    }
    _write_json(directory / "manifest.json", manifest)
    _write_json(
        state_path,
        {"current_version": version, "enabled": True},
    )
    return manifest


def _active_tool(name: str) -> tuple[dict[str, Any], str, Path]:
    name = _safe_name(name)
    approved = _approved_root() / name
    state = _read_json(approved / "state.json")
    if not state.get("enabled", False):
        raise ToolError(f"dynamic tool {name!r} is disabled")
    version = int(state.get("current_version", 0))
    directory = approved / f"v{version}"
    manifest = _read_json(directory / "manifest.json")
    source_path = directory / "tool.py"
    code = source_path.read_text(encoding="utf-8")
    if manifest.get("sha256") != _sha256(code.encode("utf-8")):
        raise ToolError(f"dynamic tool {name!r} failed its hash check")
    return manifest, code, source_path


def list_approved() -> list[dict[str, Any]]:
    root = _approved_root()
    if not root.is_dir():
        return []
    rows = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or not _NAME_RE.fullmatch(path.name):
            continue
        try:
            state = _read_json(path / "state.json")
            version = int(state.get("current_version", 0))
            manifest = _read_json(path / f"v{version}" / "manifest.json")
            rows.append({**manifest, "enabled": bool(state.get("enabled"))})
        except ToolError:
            continue
    return rows


def disable_tool(name: str) -> None:
    name = _safe_name(name)
    path = _approved_root() / name / "state.json"
    state = _read_json(path)
    state["enabled"] = False
    _write_json(path, state)


def rollback_tool(name: str, version: int | None = None) -> int:
    name = _safe_name(name)
    approved = _approved_root() / name
    state = _read_json(approved / "state.json")
    current = int(state.get("current_version", 0))
    versions = sorted(
        int(path.name[1:])
        for path in approved.iterdir()
        if path.is_dir() and re.fullmatch(r"v\d+", path.name)
    )
    target = version if version is not None else next(
        (v for v in reversed(versions) if v < current), None
    )
    if target is None or target not in versions or target >= current:
        raise ToolError("no earlier approved version to roll back to")
    _read_json(approved / f"v{target}" / "manifest.json")
    state["current_version"] = target
    state["enabled"] = True
    _write_json(approved / "state.json", state)
    return target


def run_approved(name: str, args: dict[str, Any], *, timeout_s: int = 15) -> str:
    manifest, code, _source_path = _active_tool(name)
    if not isinstance(args, dict):
        raise ToolError("args must be an object")
    _validate_python(code, label="tool", require_run=True)
    harness = (
        "import importlib.util,json,sys\n"
        "spec=importlib.util.spec_from_file_location('dynamic_tool','tool.py')\n"
        "module=importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "args=json.loads(sys.stdin.read() or '{}')\n"
        "result=module.run(args)\n"
        "print(json.dumps({'tool': %r, 'result': result}, ensure_ascii=False, default=str))\n"
        % manifest["name"]
    )
    return sandbox.run(
        "python",
        harness,
        extra_files={"tool.py": code},
        stdin=json.dumps(args, ensure_ascii=False),
        timeout_s=timeout_s,
    )


_PROPOSE_SPEC = ToolSpec(
    name="propose_tool",
    description=(
        "Create a reusable, pure Python tool only when the user explicitly asks "
        "you to invent one. The tool source and selftest run in the offline "
        "Docker sandbox. Successful submissions are saved as inert proposals; "
        "they are NOT immediately callable. A human must approve them with the "
        "local CLI. No network, host files, database, WeChat, secrets, subprocess, "
        "or non-allowlisted imports."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "parameters": {"type": "object"},
            "code": {"type": "string"},
            "selftest": {"type": "string"},
        },
        "required": ["name", "description", "parameters", "code", "selftest"],
        "additionalProperties": False,
    },
)


@dataclass
class ProposeTool(Tool):
    spec = _PROPOSE_SPEC

    def call(self, args: dict[str, Any]) -> str:
        return truncate_for_llm(create_proposal(
            name=str(args.get("name") or ""),
            description=str(args.get("description") or ""),
            parameters=args.get("parameters"),
            code=str(args.get("code") or ""),
            selftest=str(args.get("selftest") or ""),
        ))


_LIST_SPEC = ToolSpec(
    name="list_dynamic_tools",
    description="List human-approved reusable tools available to run.",
    parameters={"type": "object", "properties": {}},
)


@dataclass
class ListDynamicToolsTool(Tool):
    spec = _LIST_SPEC

    def call(self, args: dict[str, Any]) -> str:
        rows = list_approved()
        if not rows:
            return "(no approved dynamic tools)"
        return "\n".join(
            f"{row['name']} v{row['version']} "
            f"enabled={str(row['enabled']).lower()} :: {row['description']}"
            for row in rows
        )


_RUN_SPEC = ToolSpec(
    name="run_dynamic_tool",
    description=(
        "Run one human-approved dynamic tool in the offline Docker sandbox. "
        "Arguments are passed as JSON and must match the tool's schema."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "args": {"type": "object"},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": 60},
        },
        "required": ["name", "args"],
        "additionalProperties": False,
    },
)


@dataclass
class RunDynamicTool(Tool):
    spec = _RUN_SPEC

    def call(self, args: dict[str, Any]) -> str:
        name = str(args.get("name") or "")
        tool_args = args.get("args")
        if not isinstance(tool_args, dict):
            raise ToolError("args must be an object")
        try:
            timeout_s = int(args.get("timeout_s", 15))
        except (TypeError, ValueError):
            timeout_s = 15
        return truncate_for_llm(
            run_approved(name, tool_args, timeout_s=timeout_s),
            limit=_MAX_OUTPUT_CHARS,
        )


def register_dynamic_tools(tools) -> None:
    tools.register(ProposeTool())
    tools.register(ListDynamicToolsTool())
    tools.register(RunDynamicTool())
