"""Capability-gated tools executed through SandboxPort."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from skill_runtime.models import SandboxPolicy
from skill_runtime.ports import SandboxPort


class ToolError(RuntimeError):
    pass


@dataclass
class ToolResult:
    ok: bool
    output: str
    meta: dict[str, Any] | None = None


class ToolLayer:
    """Owns tool implementations + capability enforcement.

    Only tools present in the capability intersection are registered.
    File writes are limited to policy.filesystem.writable; reads to
    writable ∪ readonly ∪ skill mounts.
    """

    def __init__(self, sandbox: SandboxPort) -> None:
        self._sandbox = sandbox

    def schema(self, allowed: set[str]) -> list[dict[str, Any]]:
        out = []
        for name in sorted(allowed):
            if name in _SCHEMAS:
                out.append(_SCHEMAS[name])
        # always allow finish so the loop can complete
        out.append(_SCHEMAS["finish"])
        return out

    def dispatch(
        self,
        *,
        sandbox_id: str,
        name: str,
        arguments: dict[str, Any],
        policy: SandboxPolicy,
        allowed: set[str],
    ) -> ToolResult:
        if name == "finish":
            return ToolResult(ok=True, output=json.dumps(arguments.get("artifacts") or []))
        if name not in allowed:
            raise ToolError(f"tool denied by capability intersection: {name}")
        handler = self._handlers().get(name)
        if handler is None:
            raise ToolError(f"unknown tool: {name}")
        return handler(sandbox_id=sandbox_id, args=arguments, policy=policy)

    def _handlers(self) -> dict[str, Callable[..., ToolResult]]:
        return {
            "bash": self._bash,
            "python": self._python,
            "write_file": self._write_file,
            "read_file": self._read_file,
            "list_dir": self._list_dir,
        }

    # ----- impl -----

    def _bash(self, *, sandbox_id: str, args: dict[str, Any], policy: SandboxPolicy) -> ToolResult:
        cmd = args.get("command") or ""
        if not cmd.strip():
            raise ToolError("bash: empty command")
        r = self._sandbox.exec(sandbox_id, ["bash", "-lc", cmd], timeout_sec=120)
        ok = getattr(r, "exit_code", 1) == 0
        text = (getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")
        return ToolResult(ok=ok, output=text[:20000], meta={"exit_code": getattr(r, "exit_code", None)})

    def _python(self, *, sandbox_id: str, args: dict[str, Any], policy: SandboxPolicy) -> ToolResult:
        code = args.get("code") or ""
        r = self._sandbox.exec(
            sandbox_id,
            ["python", "-c", code],
            timeout_sec=120,
        )
        ok = getattr(r, "exit_code", 1) == 0
        text = (getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")
        return ToolResult(ok=ok, output=text[:20000], meta={"exit_code": getattr(r, "exit_code", None)})

    def _write_file(self, *, sandbox_id: str, args: dict[str, Any], policy: SandboxPolicy) -> ToolResult:
        path = args.get("path") or ""
        content = args.get("content") or ""
        self._check_path(path, policy, write=True)
        self._sandbox.write_file(sandbox_id, path, content)
        return ToolResult(ok=True, output=f"wrote {path} ({len(content)} bytes)")

    def _read_file(self, *, sandbox_id: str, args: dict[str, Any], policy: SandboxPolicy) -> ToolResult:
        path = args.get("path") or ""
        self._check_path(path, policy, write=False)
        data = self._sandbox.read_file(sandbox_id, path)
        text = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else str(data)
        return ToolResult(ok=True, output=text[:20000])

    def _list_dir(self, *, sandbox_id: str, args: dict[str, Any], policy: SandboxPolicy) -> ToolResult:
        path = args.get("path") or "/"
        self._check_path(path, policy, write=False)
        r = self._sandbox.exec(sandbox_id, ["ls", "-la", path], timeout_sec=30)
        return ToolResult(ok=True, output=(getattr(r, "stdout", "") or "")[:20000])

    def _check_path(self, path: str, policy: SandboxPolicy, *, write: bool) -> None:
        if not path.startswith("/"):
            raise ToolError(f"path must be absolute: {path}")
        if write:
            allowed = policy.filesystem.writable
            if not any(path == a or path.startswith(a.rstrip("/") + "/") for a in allowed):
                raise ToolError(f"write denied outside writable dirs: {path}")
        else:
            allowed = list(policy.filesystem.writable) + list(policy.filesystem.readonly)
            if not any(path == a or path.startswith(a.rstrip("/") + "/") for a in allowed):
                raise ToolError(f"read denied outside allowed dirs: {path}")


_SCHEMAS: dict[str, dict[str, Any]] = {
    "bash": {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a bash command inside the sandbox and return stdout+stderr.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    "python": {
        "type": "function",
        "function": {
            "name": "python",
            "description": "Run a Python snippet inside the sandbox and return stdout+stderr.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a UTF-8 text file under a writable directory (default /workspace).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    "list_dir": {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a directory inside the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    "finish": {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Complete the task. Provide the list of artifact paths produced under /workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "artifacts": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                },
            },
        },
    },
}
