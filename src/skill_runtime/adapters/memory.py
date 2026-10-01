"""In-memory adapters for prototype / tests. Production swaps in E2B + object store."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field

from skill_runtime.models import (
    ExecutionRequest,
    ResolvedSkill,
    SandboxPolicy,
    SkillCapabilities,
    SkillManifest,
    SkillRef,
    TraceEvent,
    intersect_capabilities,
)
from skill_runtime.policy import SkillMount
from skill_runtime.ports import AgentRunResult, ExecResult


class InMemorySkillRegistry:
    """Skill registry with capability-intersection resolution."""

    def __init__(self, manifests: list[SkillManifest] | None = None) -> None:
        self._by_name: dict[str, dict[str, SkillManifest]] = {}
        self._files: dict[tuple[str, str], dict[str, bytes | str]] = {}
        for m in manifests or []:
            self.add(m)

    def add(
        self,
        manifest: SkillManifest,
        files: dict[str, bytes | str] | None = None,
    ) -> None:
        self._by_name.setdefault(manifest.name, {})[manifest.version] = manifest
        self._files[(manifest.name, manifest.version)] = {
            "SKILL.md": f"---\nname: {manifest.name}\n---\n{manifest.description}\n",
            **(files or {}),
        }

    def _pick(self, ref: SkillRef) -> SkillManifest:
        versions = self._by_name.get(ref.name)
        if not versions:
            raise KeyError(f"skill not found: {ref.name}")
        if ref.version in ("*", ""):
            # prototype: latest by insertion order of dict (3.7+)
            return next(reversed(versions.values()))
        if ref.version not in versions:
            raise KeyError(f"skill version not found: {ref.name}@{ref.version}")
        return versions[ref.version]

    def resolve(self, refs: list[SkillRef]) -> list[ResolvedSkill]:
        resolved = [
            ResolvedSkill(
                ref=ref,
                manifest=self._pick(ref),
                mount_path=f"/skills/{ref.name}",
            )
            for ref in refs
        ]
        # Enforce intersection at resolve time so later stages see one effective policy.
        effective = intersect_capabilities([r.manifest.capabilities for r in resolved])
        for r in resolved:
            r.manifest = r.manifest.model_copy(
                update={"capabilities": effective},
            )
        return resolved

    def bundle(self, resolved: ResolvedSkill) -> SkillMount:
        key = (resolved.ref.name, resolved.manifest.version)
        files = self._files.get(key, {})
        return SkillMount(
            name=resolved.ref.name,
            target_path=resolved.mount_path,
            files=dict(files),
            readonly=True,
        )

    def effective_capabilities(self, refs: list[SkillRef]) -> SkillCapabilities:
        return intersect_capabilities([self._pick(ref).capabilities for ref in refs])


@dataclass
class CachedEnv:
    env_ref: str
    base_image: str
    packages: tuple[str, ...]
    skill_names: tuple[str, ...]
    hit: bool = True


class LayeredEnvironment:
    """Snapshot-by-image-layer prototype.

    env key = hash(base_image + sorted packages + sorted skill names)
    Production: rebuild this against image layer cache / E2B snapshot API.
    """

    def __init__(self) -> None:
        self._cache: dict[str, CachedEnv] = {}
        self._lock = threading.Lock()
        self.build_count = 0
        self.hit_count = 0

    @staticmethod
    def _key(base_image: str, packages: list[str], skills: list[ResolvedSkill]) -> str:
        payload = "|".join(
            [
                base_image,
                ",".join(sorted(packages)),
                ",".join(sorted(s.ref.name for s in skills)),
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def prepare(
        self,
        *,
        base_image: str,
        packages: list[str],
        skills: list[ResolvedSkill],
        snapshot_policy: str,
        pinned_snapshot_id: str | None = None,
    ) -> str:
        if pinned_snapshot_id:
            return pinned_snapshot_id

        key = self._key(base_image, packages, skills)
        with self._lock:
            if snapshot_policy != "always_build" and key in self._cache:
                self.hit_count += 1
                self._cache[key].hit = True
                return self._cache[key].env_ref

            self.build_count += 1
            env_ref = f"img_{key}"
            self._cache[key] = CachedEnv(
                env_ref=env_ref,
                base_image=base_image,
                packages=tuple(sorted(packages)),
                skill_names=tuple(sorted(s.ref.name for s in skills)),
                hit=False,
            )
            return env_ref


@dataclass
class FakeSandbox:
    sandbox_id: str
    env_ref: str
    policy: SandboxPolicy
    mounts: list[SkillMount]
    destroyed: bool = False
    paused: bool = False
    commands: list[list[str]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)


class FakeSandboxManager:
    """Test double for SandboxPort (E2B adapter drops in here)."""

    def __init__(self) -> None:
        self._sandboxes: dict[str, FakeSandbox] = {}
        self._n = 0

    def create(self, env_ref: str, policy: SandboxPolicy, mounts: list[SkillMount]) -> str:
        self._n += 1
        sid = f"sbx_fake_{self._n:04d}"
        box = FakeSandbox(
            sandbox_id=sid, env_ref=env_ref, policy=policy, mounts=list(mounts)
        )
        for mount in mounts:
            root = mount.target_path.rstrip("/")
            for rel, data in mount.files.items():
                raw = data if isinstance(data, bytes) else str(data).encode()
                box.files[f"{root}/{rel.lstrip('/')}"] = raw
        self._sandboxes[sid] = box
        return sid

    def get(self, sandbox_id: str) -> FakeSandbox:
        return self._sandboxes[sandbox_id]

    def exec(self, sandbox_id: str, command: list[str], timeout_sec: int) -> ExecResult:
        box = self._sandboxes[sandbox_id]
        box.commands.append(command)
        return _Exec(exit_code=0, stdout="ok", stderr="")

    def write_file(self, sandbox_id: str, path: str, content: bytes | str) -> None:
        box = self._sandboxes[sandbox_id]
        box.files[path] = content if isinstance(content, bytes) else content.encode()

    def read_file(self, sandbox_id: str, path: str) -> bytes:
        box = self._sandboxes[sandbox_id]
        if path not in box.files:
            raise FileNotFoundError(path)
        data = box.files[path]
        return data if isinstance(data, bytes) else data.encode()

    def destroy(self, sandbox_id: str) -> None:
        self._sandboxes[sandbox_id].destroyed = True

    def pause(self, sandbox_id: str) -> None:
        self._sandboxes[sandbox_id].paused = True

    def resume(self, sandbox_id: str) -> None:
        self._sandboxes[sandbox_id].paused = False


@dataclass
class _Exec:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class _AgentRun:
    status: str
    artifacts: list[str]
    usage: dict
    error: str | None = None


class StubAgentExecutor:
    """Thin executor stub.

    Production: own tool layer (bash/python/fs) gated by effective capability
    intersection, LLM loop via model SDK. Not a full CLI agent.
    """

    def run(
        self,
        *,
        sandbox_id: str,
        request: ExecutionRequest,
        skills: list[ResolvedSkill],
        effective_policy: SandboxPolicy,
    ) -> AgentRunResult:
        if not skills:
            return _AgentRun(
                status="failed", artifacts=[], usage={}, error="no skills mounted"
            )
        tools_allowed = set.intersection(*[set(s.manifest.capabilities.tools) for s in skills])
        return _AgentRun(
            status="ok" if tools_allowed else "failed",
            artifacts=list(request.task.outputs) if tools_allowed else [],
            usage={"llm_tokens": 0, "tool_calls": 0},
            error=None if tools_allowed else "capability intersection empty (tools)",
        )


class InMemoryTraceSink:
    def __init__(self) -> None:
        self._events: dict[str, list[TraceEvent]] = {}
        self._lock = threading.Lock()

    def emit(self, event: TraceEvent) -> None:
        with self._lock:
            self._events.setdefault(event.execution_id, []).append(event)

    def list_events(self, execution_id: str) -> list[TraceEvent]:
        return list(self._events.get(execution_id, []))
