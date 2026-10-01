"""Thin agent executor: LLM loop + capability-gated tools inside the sandbox."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from skill_runtime.agent.llm import ChatMessage, LLMError, OpenAICompatClient
from skill_runtime.agent.tools import ToolError, ToolLayer
from skill_runtime.models import ExecutionRequest, ResolvedSkill, SandboxPolicy
from skill_runtime.ports import SandboxPort


@dataclass
class AgentRun:
    status: str  # ok | failed | timeout
    artifacts: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)


class ToolGatedAgentExecutor:
    """Owns the tool layer (capability gate) and the LLM tool-calling loop.

    Not a full CLI agent: model calls happen on the control plane; tool effects
    execute inside the microVM via SandboxPort.
    """

    def __init__(
        self,
        llm: OpenAICompatClient,
        sandbox: SandboxPort,
        *,
        system_prompt: str | None = None,
    ) -> None:
        self.llm = llm
        self.sandbox = sandbox
        self.tools = ToolLayer(sandbox)
        self.system_prompt = system_prompt or _DEFAULT_SYSTEM
        self.last_run: AgentRun | None = None

    def run(
        self,
        *,
        sandbox_id: str,
        request: ExecutionRequest,
        skills: list[ResolvedSkill],
        effective_policy: SandboxPolicy,
    ) -> AgentRun:
        allowed = self._allowed_tools(skills)
        tool_schema = self.tools.schema(allowed)
        messages = self._build_messages(request, skills)

        max_steps = max(1, request.agent.max_steps)
        deadline = time.time() + max(30, request.agent.timeout_sec)
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "tool_calls": 0}
        steps: list[dict[str, Any]] = []
        artifacts: list[str] = []

        def _finish(status: str, *, error: str | None = None) -> AgentRun:
            run = AgentRun(
                status=status,
                artifacts=artifacts,
                usage=usage,
                error=error,
                steps=steps,
                messages=[m.to_api() for m in messages],
            )
            self.last_run = run
            return run

        for step in range(max_steps):
            if time.time() > deadline:
                return _finish("timeout", error=f"agent timeout after {step} steps")

            try:
                result = self.llm.chat(messages, tools=tool_schema)
            except LLMError as exc:
                return _finish("failed", error=str(exc))

            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                usage[k] += int(result.usage.get(k) or 0)

            msg = result.message
            messages.append(msg)

            if not msg.tool_calls:
                # final answer without finish() — accept prose as summary
                steps.append({"type": "final", "content": (msg.content or "")[:500]})
                if not artifacts:
                    artifacts = self._default_artifacts(request)
                return _finish("ok")

            for call in msg.tool_calls:
                usage["tool_calls"] += 1
                name, args, call_id = _parse_tool_call(call)
                step_rec: dict[str, Any] = {"type": "tool", "tool": name, "args": args}
                try:
                    if name == "finish":
                        self.tools.dispatch(
                            sandbox_id=sandbox_id,
                            name="finish",
                            arguments=args,
                            policy=effective_policy,
                            allowed=allowed,
                        )
                        artifacts = _as_str_list(args.get("artifacts")) or self._default_artifacts(
                            request
                        )
                        step_rec["ok"] = True
                        steps.append(step_rec)
                        return _finish("ok")

                    tr = self.tools.dispatch(
                        sandbox_id=sandbox_id,
                        name=name,
                        arguments=args,
                        policy=effective_policy,
                        allowed=allowed,
                    )
                    step_rec["ok"] = tr.ok
                    step_rec["output_preview"] = tr.output[:300]
                    steps.append(step_rec)
                    tool_content = tr.output if tr.ok else f"ERROR: {tr.output}"
                except ToolError as exc:
                    step_rec["ok"] = False
                    step_rec["error"] = str(exc)
                    steps.append(step_rec)
                    tool_content = f"ERROR: {exc}"

                messages.append(
                    ChatMessage(
                        role="tool",
                        content=tool_content[:20000],
                        tool_call_id=call_id,
                        name=name,
                    )
                )

        return _finish(
            "failed",
            error=f"max_steps ({max_steps}) exceeded without finish()",
        )

    # ----- helpers -----

    @staticmethod
    def _allowed_tools(skills: list[ResolvedSkill]) -> set[str]:
        if not skills:
            return set()
        # capabilities already intersected on every resolved skill
        return set(skills[0].manifest.capabilities.tools) & {
            "bash",
            "python",
            "write_file",
            "read_file",
            "list_dir",
        }

    def _build_messages(
        self, request: ExecutionRequest, skills: list[ResolvedSkill]
    ) -> list[ChatMessage]:
        parts = [self.system_prompt]
        if skills:
            parts.append("## Mounted skills (read-only under /skills)")
            for s in skills:
                parts.append(
                    f"- {s.ref.name}@{s.manifest.version} → {s.mount_path}\n"
                    f"  entry: {s.manifest.entry}\n"
                    f"  desc: {s.manifest.description}"
                )
            parts.append(
                "Read a skill's SKILL.md via read_file before using its procedures."
            )
        parts.append("## Task")
        parts.append(request.task.prompt)
        if request.task.outputs:
            parts.append("Required output artifacts (absolute paths under /workspace):")
            parts.extend(f"- {p}" for p in request.task.outputs)
        parts.append("Use tools to complete the task, then call finish() with artifact paths.")
        return [ChatMessage(role="system", content="\n\n".join(parts))]

    @staticmethod
    def _default_artifacts(request: ExecutionRequest) -> list[str]:
        return list(request.task.outputs)


def _parse_tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    call_id = call.get("id") or ""
    fn = call.get("function") or {}
    name = fn.get("name") or ""
    raw_args = fn.get("arguments") or "{}"
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            args = {"_raw": raw_args}
    else:
        args = dict(raw_args)
    return name, args, call_id


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


_DEFAULT_SYSTEM = """You are a careful software agent running inside a sandboxed Linux VM.

Rules:
- Prefer small, verifiable steps.
- Write outputs under /workspace only.
- Skills are mounted read-only under /skills; read SKILL.md first when relevant.
- Network access is restricted; do not rely on arbitrary internet hosts.
- When done, call finish() with the list of artifact paths you produced.
"""
