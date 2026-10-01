"""Minimal OpenAI-compatible chat client (tool calling)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_api(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            d["content"] = self.content
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class ChatResult:
    message: ChatMessage
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""


class LLMError(RuntimeError):
    pass


class OpenAICompatClient:
    """POST {base_url}/chat/completions with Bearer auth."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = "mimo-v2.6-pro",
        timeout_sec: int = 120,
    ) -> None:
        self.base_url = (base_url or os.environ.get("SKILL_RUNTIME_LLM_BASE") or "").rstrip("/")
        self.api_key = api_key or os.environ.get("SKILL_RUNTIME_LLM_KEY") or ""
        self.model = model or os.environ.get("SKILL_RUNTIME_LLM_MODEL") or "mimo-v2.6-pro"
        self.timeout_sec = timeout_sec
        if not self.base_url or not self.api_key:
            raise LLMError("LLM base_url/api_key not configured")

    @classmethod
    def from_mimocode_auth(
        cls,
        *,
        model: str = "mimo-v2.6-pro",
        auth_path: str | None = None,
    ) -> OpenAICompatClient:
        """Load xiaomi OpenAI-compatible credentials from MiMoCode auth.json."""
        path = auth_path or os.path.expanduser("~/.local/share/mimocode/auth.json")
        with open(path) as f:
            data = json.load(f)
        node = data.get("xiaomi") or {}
        meta = node.get("metadata") or {}
        return cls(
            base_url=meta.get("base_url"),
            api_key=node.get("key"),
            model=model,
        )

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_api() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        body = self._post("/chat/completions", payload)
        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = body.get("usage") or {}
        return ChatResult(
            message=ChatMessage(
                role=msg.get("role") or "assistant",
                content=msg.get("content"),
                tool_calls=msg.get("tool_calls"),
            ),
            usage={
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
            model=body.get("model") or self.model,
        )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise LLMError(f"LLM HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"LLM connection error: {exc}") from exc
