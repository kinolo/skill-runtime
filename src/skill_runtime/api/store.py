"""Execution store + background runner for the HTTP API."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from skill_runtime.ids import execution_id as gen_execution_id
from skill_runtime.models import ExecutionRecord, ExecutionRequest, ExecutionResult
from skill_runtime.orchestrator import Orchestrator
from skill_runtime.state_machine import ExecutionState

logger = logging.getLogger(__name__)


class ExecutionNotFound(KeyError):
    pass


class ExecutionStore:
    """In-memory store wrapping Orchestrator."""

    def __init__(self, orchestrator: Orchestrator, *, max_workers: int = 8) -> None:
        self.orchestrator = orchestrator
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="skill-exec")
        self._futures: dict[str, Future[ExecutionResult]] = {}
        self._webhooks: dict[str, str | None] = {}
        self._lock = threading.Lock()
        self._on_finish: list[Callable[[str, ExecutionResult], None]] = []

    def on_finish(self, cb: Callable[[str, ExecutionResult], None]) -> None:
        self._on_finish.append(cb)

    def set_webhook(self, execution_id: str, webhook: str | None) -> None:
        with self._lock:
            self._webhooks[execution_id] = webhook

    def submit_sync(self, request: ExecutionRequest, *, execution_id: str | None = None) -> ExecutionResult:
        result = self.orchestrator.submit(request, execution_id=execution_id)
        self._notify(result.execution_id, result)
        return result

    def submit_async(self, request: ExecutionRequest) -> str:
        eid = gen_execution_id()
        fut = self._pool.submit(self._run, eid, request)
        with self._lock:
            self._futures[eid] = fut
        return eid

    def _run(self, eid: str, request: ExecutionRequest) -> ExecutionResult:
        try:
            result = self.orchestrator.submit(request, execution_id=eid)
        except Exception as exc:
            logger.exception("execution %s failed", eid)
            result = ExecutionResult(
                execution_id=eid,
                status=ExecutionState.FAILED,
                trace_id="",
                error=str(exc),
            )
        self._notify(eid, result)
        return result

    def get_record(self, execution_id: str) -> ExecutionRecord:
        try:
            return self.orchestrator.get(execution_id)
        except KeyError as exc:
            raise ExecutionNotFound(execution_id) from exc

    def get_result(self, execution_id: str) -> ExecutionResult:
        record = self.get_record(execution_id)
        if record.result is not None:
            return record.result
        fut = self._futures.get(execution_id)
        if fut is not None:
            if not fut.done():
                raise ExecutionNotReady(execution_id)
            exc = fut.exception()
            if exc is not None:
                return ExecutionResult(
                    execution_id=execution_id,
                    status=ExecutionState.FAILED,
                    trace_id=record.trace_id or "",
                    error=str(exc),
                )
            return fut.result()
        raise ExecutionNotFound(execution_id)

    def cancel(self, execution_id: str) -> ExecutionRecord:
        return self.orchestrator.cancel(execution_id)

    def replay(self, execution_id: str) -> ExecutionResult:
        record = self.get_record(execution_id)
        return self.submit_sync(record.request)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _notify(self, execution_id: str, result: ExecutionResult) -> None:
        for cb in self._on_finish:
            try:
                cb(execution_id, result)
            except Exception:
                logger.exception("on_finish hook failed for %s", execution_id)
        webhook = self._webhooks.get(execution_id)
        if webhook:
            _post_webhook(webhook, result)


class ExecutionNotReady(RuntimeError):
    def __init__(self, execution_id: str) -> None:
        super().__init__(f"execution still running: {execution_id}")
        self.execution_id = execution_id


def _post_webhook(url: str, result: ExecutionResult) -> None:
    import json
    import urllib.request

    data = json.dumps(result.model_dump(mode="json")).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001
        logger.warning("webhook post failed: %s", url)
