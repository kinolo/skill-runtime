"""ID generation helpers."""

from __future__ import annotations

import secrets
import time


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def execution_id() -> str:
    return new_id("exec")


def trace_id() -> str:
    return new_id("tr")


def span_id() -> str:
    return new_id("sp")


def sandbox_id() -> str:
    return new_id("sbx")


def now_ms() -> int:
    return int(time.time() * 1000)
