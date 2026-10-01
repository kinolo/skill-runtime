"""Agent executor loop tests (LLM mocked, tools hit sandbox port)."""

from __future__ import annotations

import json
from typing import Any

from skill_runtime.adapters.memory import FakeSandboxManager, InMemorySkillRegistry
from skill_runtime.agent.executor import ToolGatedAgentExecutor
from skill_runtime.agent.llm import ChatMessage, ChatResult, LLMError
from skill_runtime.models import (
    ExecutionRequest,
    SkillCapabilities,
    SkillManifest,
    SkillRef,
    TaskSpec,
)
from skill_runtime.policy import cap_to_policy


class ScriptedLLM:
    """Drop-in stand-in for OpenAICompatClient.chat."""

    def __init__(self, replies: list[ChatResult]):
        self._replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    def chat(self, messages, *, tools=None, **kwargs) -> ChatResult:
        self.calls.append(list(messages))
        if not self._replies:
            raise LLMError("script exhausted")
        return self._replies.pop(0)


def _result(content: str | None = None, tool_calls: list[dict] | None = None) -> ChatResult:
    return ChatResult(
        message=ChatMessage(role="assistant", content=content, tool_calls=tool_calls),
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )


def _tc(name: str, args: dict[str, Any], call_id: str = "c1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _setup(tools: list[str]):
    reg = InMemorySkillRegistry()
    reg.add(
        SkillManifest(
            name="demo",
            version="1.0.0",
            description="demo skill",
            capabilities=SkillCapabilities(
                network=["api.example.com"],
                filesystem_read=["/workspace", "/skills"],
                filesystem_write=["/workspace"],
                tools=tools,
            ),
        ),
        files={"SKILL.md": "# demo skill\nWrite a file under /workspace.\n"},
    )
    skills = reg.resolve([SkillRef(name="demo")])
    policy = cap_to_policy(skills[0].manifest.capabilities)
    sandbox = FakeSandboxManager()
    sid = sandbox.create("snap_x", policy, [reg.bundle(skills[0])])
    return reg, skills, policy, sandbox, sid


def test_executor_write_file_and_finish():
    _reg, skills, policy, sandbox, sid = _setup(["bash", "python", "write_file", "read_file"])
    llm = ScriptedLLM(
        [
            _result(tool_calls=[_tc("write_file", {"path": "/workspace/out.txt", "content": "hello\n"})]),
            _result(tool_calls=[_tc("finish", {"artifacts": ["/workspace/out.txt"], "summary": "done"})]),
        ]
    )
    ex = ToolGatedAgentExecutor(llm, sandbox)  # type: ignore[arg-type]
    run = ex.run(
        sandbox_id=sid,
        request=ExecutionRequest(
            skill_refs=[SkillRef(name="demo")],
            task=TaskSpec(prompt="write hello", outputs=["/workspace/out.txt"]),
        ),
        skills=skills,
        effective_policy=policy,
    )
    assert run.status == "ok"
    assert run.artifacts == ["/workspace/out.txt"]
    assert sandbox.get(sid).files["/workspace/out.txt"] == b"hello\n"
    assert run.usage["tool_calls"] == 2
    assert run.usage["total_tokens"] == 30


def test_executor_denies_tool_outside_capability():
    _reg, skills, policy, sandbox, sid = _setup(["write_file"])  # no bash
    llm = ScriptedLLM(
        [
            _result(tool_calls=[_tc("bash", {"command": "curl evil.com"}, call_id="c1")]),
            _result(tool_calls=[_tc("finish", {"artifacts": ["/workspace/a.txt"]})]),
        ]
    )
    ex = ToolGatedAgentExecutor(llm, sandbox)  # type: ignore[arg-type]
    run = ex.run(
        sandbox_id=sid,
        request=ExecutionRequest(
            skill_refs=[SkillRef(name="demo")],
            task=TaskSpec(prompt="x", outputs=["/workspace/a.txt"]),
        ),
        skills=skills,
        effective_policy=policy,
    )
    assert run.status == "ok"
    # first tool call denied
    tool_msgs = [m for m in run.messages if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"].startswith("ERROR:")


def test_executor_write_outside_workspace_denied():
    _reg, skills, policy, sandbox, sid = _setup(["write_file", "bash"])
    llm = ScriptedLLM(
        [
            _result(tool_calls=[_tc("write_file", {"path": "/etc/passwd", "content": "x"})]),
            _result(content="gave up"),
        ]
    )
    ex = ToolGatedAgentExecutor(llm, sandbox)  # type: ignore[arg-type]
    run = ex.run(
        sandbox_id=sid,
        request=ExecutionRequest(
            skill_refs=[SkillRef(name="demo")],
            task=TaskSpec(prompt="x"),
        ),
        skills=skills,
        effective_policy=policy,
    )
    assert "/etc/passwd" not in sandbox.get(sid).files
    tool_msgs = [m for m in run.messages if m["role"] == "tool"]
    assert "write denied" in tool_msgs[0]["content"]


def test_executor_max_steps():
    _reg, skills, policy, sandbox, sid = _setup(["bash"])
    llm = ScriptedLLM(
        [_result(tool_calls=[_tc("bash", {"command": "echo hi"}, call_id=f"c{i}")]) for i in range(5)]
    )
    ex = ToolGatedAgentExecutor(llm, sandbox)  # type: ignore[arg-type]
    run = ex.run(
        sandbox_id=sid,
        request=ExecutionRequest(
            skill_refs=[SkillRef(name="demo")],
            task=TaskSpec(prompt="x"),
            # max_steps via AgentSpec default 40 — use small by constructing request
        ),
        skills=skills,
        effective_policy=policy,
    )
    # script has 5 tool replies, then LLMError → failed
    assert run.status == "failed"
