# -*- coding: utf-8 -*-
"""Run model-written code in a throwaway Docker container.

Threat model
------------
The code is written by a language model, so treat it as untrusted: it may loop
forever, allocate wildly, or try to reach the network. The container is the
boundary, and these are the properties that matter:

  --network none              no egress; the program cannot call out
  --memory / --memory-swap    no swap escape; the OOM killer is confined
  --cpus / --pids-limit       no host starvation, no fork bomb
  --cap-drop ALL              no CAP_SYS_ADMIN & friends
  no-new-privileges           setuid binaries inside cannot escalate
  no host path mounted        files arrive via `docker cp`, so the host tree
                              is never exposed and never written by the
                              container

The Docker socket is never mounted, so the container cannot create siblings.

Why `docker cp` and not a bind mount
------------------------------------
A bind mount is one fewer step, but Docker Desktop for Windows answered
"CreateFile C:\\...: Access is denied" when the daemon user and the CLI user
differ, which is exactly the case when the agent runs from a service-like
session. `docker cp` goes through the daemon and sidesteps host file sharing
entirely -- and it is strictly better for isolation, since the container keeps
no handle on the host filesystem.

Timeouts are enforced twice: `timeout -k` inside the container (clean exit) and
a host-side `subprocess` timeout (backstop if the container wedges), followed by
an explicit `docker kill`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid

IMAGE = "occamy-sandbox:latest"
CONTAINER_WORKDIR = "/tmp"
MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT_S = 15
HARD_TIMEOUT_EXTRA_S = 20
MEMORY_LIMIT = "512m"
CPU_LIMIT = "1.0"
PIDS_LIMIT = "64"

_ENTRY = {"cpp": "main.cpp", "c": "main.c", "python": "main.py"}


class SandboxError(RuntimeError):
    """Raised when the sandbox itself could not run (docker missing, image
    absent). Distinct from the program failing, which is a normal result."""


def _docker() -> str:
    exe = shutil.which("docker")
    if not exe:
        raise SandboxError("docker not found on PATH")
    return exe


def _compile_cmd(language: str, entry: str) -> str:
    if language in ("cpp", "c"):
        compiler = "g++" if language == "cpp" else "gcc"
        std = " -std=c++17" if language == "cpp" else " -std=c11"
        # Compile first so a compiler error is not confused with a runtime one.
        return '%s -O2%s -o prog "%s" && ./prog' % (compiler, std, entry)
    return 'python3 "%s"' % entry


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + (
        "\n...[output truncated, total %d chars]" % len(text))


def run(
    language: str,
    code: str,
    extra_files: dict[str, str] | None = None,
    stdin: str = "",
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> str:
    """Compile (if needed) and run `code`, returning a report for the model."""
    language = (language or "").strip().lower()
    if language not in _ENTRY:
        raise SandboxError("language must be one of: cpp, c, python")
    if not isinstance(code, str) or not code.strip():
        raise SandboxError("code must be a non-empty string")

    timeout_s = max(1, min(int(timeout_s or DEFAULT_TIMEOUT_S), 60))
    entry = _ENTRY[language]

    workdir = tempfile.mkdtemp(prefix="occamy_sbx_")
    name = "occamy-sbx-" + uuid.uuid4().hex[:12]
    docker = _docker()
    try:
        with open(os.path.join(workdir, entry), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write(code)
        for fname, content in (extra_files or {}).items():
            safe = os.path.basename(str(fname))
            if not safe or safe == entry:
                continue
            with open(os.path.join(workdir, safe), "w", encoding="utf-8",
                      newline="\n") as f:
                f.write(str(content))

        inner = _compile_cmd(language, entry)
        if stdin:
            # Feeding stdin through `docker start -ai` is unreliable once the
            # CLI has already attached; a file redirect is deterministic.
            with open(os.path.join(workdir, "stdin.txt"), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write(stdin)
            inner += " < stdin.txt"
        create = [
            docker, "create", "--name", name,
            "--network", "none",
            "--memory", MEMORY_LIMIT,
            "--memory-swap", MEMORY_LIMIT,
            "--cpus", CPU_LIMIT,
            "--pids-limit", PIDS_LIMIT,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-w", CONTAINER_WORKDIR,
            IMAGE,
            "timeout", "-k", "2", "%ds" % timeout_s, "sh", "-c", inner,
        ]
        created = subprocess.run(create, capture_output=True, timeout=120)
        if created.returncode != 0:
            raise SandboxError("docker create failed: %s"
                               % (created.stderr or b"").decode("utf-8", "replace")[:300])

        copied = subprocess.run(
            [docker, "cp", workdir + os.sep + ".", "%s:%s" % (name, CONTAINER_WORKDIR)],
            capture_output=True, timeout=120)
        if copied.returncode != 0:
            raise SandboxError("docker cp failed: %s"
                               % (copied.stderr or b"").decode("utf-8", "replace")[:300])

        try:
            proc = subprocess.run(
                [docker, "start", "-a", name],
                capture_output=True,
                timeout=timeout_s + HARD_TIMEOUT_EXTRA_S,
            )
        except subprocess.TimeoutExpired:
            subprocess.run([docker, "kill", name], capture_output=True, timeout=30)
            return "TIMEOUT: the program did not finish within %ds" % timeout_s

        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")

        code_rc = None
        try:
            code_rc = int(subprocess.check_output(
                [docker, "inspect", "--format", "{{.State.ExitCode}}", name],
                timeout=60).decode().strip())
        except Exception:
            code_rc = proc.returncode

        parts = []
        if out.strip():
            parts.append("stdout:\n" + out.rstrip())
        if err.strip():
            parts.append("stderr:\n" + err.rstrip())
        if code_rc == 124:
            parts.append("(killed by timeout: exceeded %ds)" % timeout_s)
        elif code_rc == 137:
            parts.append("(killed: out of memory or SIGKILL)")
        parts.append("exit code: %d" % (code_rc if code_rc is not None else -1))
        return _truncate("\n".join(parts))
    finally:
        try:
            subprocess.run([docker, "rm", "-f", name],
                           capture_output=True, timeout=60)
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


def ensure_image() -> str:
    """Return 'ok'; raise SandboxError if the image is missing."""
    exe = _docker()
    proc = subprocess.run([exe, "image", "inspect", IMAGE],
                          capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise SandboxError("image %s not built yet" % IMAGE)
    return "ok"
