"""E2B adapter: SandboxPort + EnvironmentPort over microVM snapshots.

Design (locked):
  - Snapshot granularity = image layers, key = hash(base_image + packages + skills)
  - Skill files are baked into the env snapshot at prepare() and re-mounted
    read-only on create() so mounts are consistent even on cache hits.
  - Capability intersection → E2B network allowlist + FS chmod locks.

The e2b SDK is imported lazily so unit tests can inject a fake client.
"""

from __future__ import annotations

import hashlib
import logging
import shlex
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from skill_runtime.models import ResolvedSkill, SandboxPolicy
from skill_runtime.policy import SkillMount, network_config_from_policy

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE = "/workspace"
DEFAULT_SKILLS_ROOT = "/skills"


# ----- client surface (real e2b.Sandbox or test double) -----


@runtime_checkable
class E2BSandboxClient(Protocol):
    sandbox_id: str
    filesystem: Any
    commands: Any

    def kill(self) -> bool: ...
    def pause(self, keep_memory: bool | None = None) -> bool: ...


class E2BClientFactory(Protocol):
    def __call__(self, **kwargs: Any) -> E2BSandboxClient: ...


@dataclass
class E2BCreateResult:
    sandbox: E2BSandboxClient
    network: dict[str, Any]
    allow_internet_access: bool


@dataclass
class E2BSandboxHandle:
    sandbox_id: str
    env_ref: str
    policy: SandboxPolicy
    mounts: list[SkillMount]
    paused: bool = False
    destroyed: bool = False


@dataclass
class LayerKeyParts:
    base_image: str
    packages: tuple[str, ...]
    skill_fingerprint: str

    def key(self) -> str:
        payload = "|".join(
            [
                self.base_image,
                ",".join(self.packages),
                self.skill_fingerprint,
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def skill_fingerprint(mounts: list[SkillMount]) -> str:
    """Stable hash of skill file trees (path + content)."""
    h = hashlib.sha256()
    for m in sorted(mounts, key=lambda x: x.name):
        h.update(m.name.encode())
        h.update(m.target_path.encode())
        for rel in sorted(m.files):
            h.update(rel.encode())
            data = m.files[rel]
            h.update(data if isinstance(data, bytes) else data.encode())
    return h.hexdigest()[:16]


def layer_key(base_image: str, packages: list[str], mounts: list[SkillMount]) -> str:
    parts = LayerKeyParts(
        base_image=base_image,
        packages=tuple(sorted(packages)),
        skill_fingerprint=skill_fingerprint(mounts),
    )
    return parts.key()


def snapshot_alias(key: str) -> str:
    # E2B snapshot/template names: lowercase, no underscores preferred
    return f"sr-env-{key}"


# ----- EnvironmentPort -----


class E2BEnvironment:
    """Image-layer snapshot environment builder.

    prepare() returns an env_ref usable as Sandbox.create(template=...):
      - cache hit  → existing snapshot/template id
      - cache miss → bake: boot base image, install packages, write skills
                     read-only, create_snapshot(name=sr-env-<key>)
    """

    def __init__(
        self,
        *,
        create_sandbox: E2BClientFactory | None = None,
        snapshot_lookup: Any | None = None,
        snapshot_create: Any | None = None,
        default_base_image: str = "base",
    ) -> None:
        self._create_sandbox = create_sandbox
        self._snapshot_lookup = snapshot_lookup
        self._snapshot_create = snapshot_create
        self.default_base_image = default_base_image
        self._local_cache: dict[str, str] = {}  # key -> snapshot_id
        self.build_count = 0
        self.hit_count = 0

    def prepare(
        self,
        *,
        base_image: str,
        packages: list[str],
        skills: list[ResolvedSkill],
        snapshot_policy: str,
        pinned_snapshot_id: str | None = None,
    ) -> str:
        # ResolvedSkill list is metadata-only here; file content comes from
        # SandboxPort mounts. Layer key uses skill name@version identity so
        # identity changes bust the cache; content hash is applied in the
        # sandbox adapter's mount step.
        mounts = [
            SkillMount(name=s.ref.name, target_path=s.mount_path, files={})
            for s in skills
        ]
        return self.prepare_mounts(
            base_image=base_image,
            packages=packages,
            mounts=mounts,
            snapshot_policy=snapshot_policy,
            pinned_snapshot_id=pinned_snapshot_id,
        )

    def prepare_mounts(
        self,
        *,
        base_image: str,
        packages: list[str],
        mounts: list[SkillMount],
        snapshot_policy: str,
        pinned_snapshot_id: str | None = None,
    ) -> str:
        if pinned_snapshot_id:
            return pinned_snapshot_id

        key = layer_key(base_image, packages, mounts)
        alias = snapshot_alias(key)

        if snapshot_policy != "always_build" and key in self._local_cache:
            self.hit_count += 1
            return self._local_cache[key]

        # remote lookup (template/snapshot already baked)
        if snapshot_policy != "always_build" and self._snapshot_lookup is not None:
            existing = self._snapshot_lookup(alias)
            if existing:
                self.hit_count += 1
                self._local_cache[key] = existing
                return existing

        env_ref = self._bake(base_image, packages, mounts, alias)
        self.build_count += 1
        self._local_cache[key] = env_ref
        return env_ref

    def _bake(
        self,
        base_image: str,
        packages: list[str],
        mounts: list[SkillMount],
        alias: str,
    ) -> str:
        if self._create_sandbox is None:
            # No E2B client: return deterministic fake ref (tests / dry-run).
            return f"snap_{alias}"

        sbx = self._create_sandbox(template=base_image or self.default_base_image)
        try:
            if packages:
                cmd = self._install_cmd(packages)
                if cmd:
                    sbx.commands.run(cmd)
            _install_skill_files(sbx, mounts, readonly=True)
            _ensure_workspace(sbx)
            if self._snapshot_create is not None:
                info = self._snapshot_create(sbx, name=alias)
                return str(info)
            return alias
        finally:
            try:
                sbx.kill()
            except Exception:  # noqa: BLE001
                logger.warning("failed to kill bake sandbox %s", getattr(sbx, "sandbox_id", "?"))

    @staticmethod
    def _install_cmd(packages: list[str]) -> str | None:
        apt = [p for p in packages if not p.startswith("pip:")]
        pip = [p.removeprefix("pip:") for p in packages if p.startswith("pip:")]
        parts = []
        if apt:
            parts.append(
                "export DEBIAN_FRONTEND=noninteractive; "
                f"apt-get update -qq && apt-get install -y -qq {' '.join(shlex.quote(p) for p in apt)}"
            )
        if pip:
            parts.append(f"pip install -q {' '.join(shlex.quote(p) for p in pip)}")
        return " && ".join(parts) or None


# ----- SandboxPort -----


class E2BSandbox:
    """SandboxPort over e2b microVMs.

    create():
      1. Boot from env_ref (template or snapshot_id) with capability network policy
      2. Mount skill bundles under /skills (read-only)
      3. Ensure writable /workspace for outputs
    """

    def __init__(
        self,
        *,
        create_sandbox: E2BClientFactory | None = None,
        connect_sandbox: Any | None = None,
        timeout_sec: int = 3600,
    ) -> None:
        self._create_sandbox = create_sandbox
        self._connect_sandbox = connect_sandbox
        self._timeout = timeout_sec
        self._live: dict[str, E2BSandboxHandle] = {}
        self._clients: dict[str, E2BSandboxClient] = {}
        self.created_count = 0

    def create(self, env_ref: str, policy: SandboxPolicy, mounts: list[SkillMount]) -> str:
        net = network_config_from_policy(policy)
        sbx = self._boot(env_ref, net)
        sid = sbx.sandbox_id

        _ensure_workspace(sbx, extra_writable=policy.filesystem.writable)
        _install_skill_files(sbx, mounts, readonly=True)
        _enforce_fs_policy(sbx, policy)

        self._clients[sid] = sbx
        self._live[sid] = E2BSandboxHandle(
            sandbox_id=sid,
            env_ref=env_ref,
            policy=policy,
            mounts=list(mounts),
        )
        self.created_count += 1
        return sid

    def _boot(self, env_ref: str, net) -> E2BSandboxClient:
        if self._create_sandbox is None:
            raise RuntimeError("E2B client factory not configured")
        return self._create_sandbox(
            template=env_ref,
            timeout=self._timeout,
            allow_internet_access=net.allow_internet_access,
            network=net.network,
        )

    def exec(self, sandbox_id: str, command: list[str], timeout_sec: int) -> Any:
        sbx = self._require(sandbox_id)
        joined = " ".join(shlex.quote(c) for c in command)
        result = sbx.commands.run(joined, timeout=timeout_sec)

        class _R:
            exit_code = getattr(result, "exit_code", 0)
            stdout = getattr(result, "stdout", "")
            stderr = getattr(result, "stderr", "")

        return _R()

    def write_file(self, sandbox_id: str, path: str, content: bytes | str) -> None:
        sbx = self._require(sandbox_id)
        parent = path.rsplit("/", 1)[0]
        if parent and parent not in ("/", ""):
            try:
                sbx.filesystem.make_dir(parent)
            except Exception:  # noqa: BLE001
                logger.debug("make_dir %s failed", parent)
        sbx.filesystem.write(path, content)

    def read_file(self, sandbox_id: str, path: str) -> bytes:
        sbx = self._require(sandbox_id)
        data = sbx.filesystem.read(path, format="bytes")
        return data if isinstance(data, (bytes, bytearray)) else str(data).encode()

    def destroy(self, sandbox_id: str) -> None:
        sbx = self._clients.pop(sandbox_id, None)
        handle = self._live.pop(sandbox_id, None)
        if handle:
            handle.destroyed = True
        if sbx is not None:
            try:
                sbx.kill()
            except Exception:  # noqa: BLE001
                logger.warning("kill failed for %s", sandbox_id)

    def pause(self, sandbox_id: str) -> None:
        sbx = self._require(sandbox_id)
        sbx.pause(keep_memory=True)
        if sandbox_id in self._live:
            self._live[sandbox_id].paused = True

    def resume(self, sandbox_id: str) -> None:
        if self._connect_sandbox is None:
            raise RuntimeError("connect_sandbox factory not configured")
        sbx = self._connect_sandbox(sandbox_id)
        self._clients[sandbox_id] = sbx
        if sandbox_id in self._live:
            self._live[sandbox_id].paused = False

    def get(self, sandbox_id: str) -> E2BSandboxHandle:
        return self._live[sandbox_id]

    def _require(self, sandbox_id: str) -> E2BSandboxClient:
        if sandbox_id not in self._clients:
            raise KeyError(f"sandbox not found: {sandbox_id}")
        return self._clients[sandbox_id]


# ----- shared mount / fs helpers (usable with real or fake client) -----


def _install_skill_files(
    sbx: E2BSandboxClient,
    mounts: list[SkillMount],
    *,
    readonly: bool,
) -> None:
    for mount in mounts:
        root = mount.target_path.rstrip("/")
        sbx.filesystem.make_dir(root)
        for rel, data in mount.files.items():
            dest = f"{root}/{rel.lstrip('/')}"
            parent = dest.rsplit("/", 1)[0]
            if parent and parent != root:
                # best-effort nested dirs
                sbx.filesystem.make_dir(parent)
            sbx.filesystem.write(dest, data)
        if mount.readonly or readonly:
            _chmod_tree(sbx, root, "a-w")


def _ensure_workspace(sbx: E2BSandboxClient, extra_writable: list[str] | None = None) -> None:
    paths = {DEFAULT_WORKSPACE, *(extra_writable or [])}
    for path in sorted(paths):
        if not path or path == "/":
            continue
        try:
            sbx.filesystem.make_dir(path)
        except Exception:  # noqa: BLE001
            logger.debug("make_dir %s failed (may exist)", path)
        _chmod_tree(sbx, path, "u+w")


def _enforce_fs_policy(sbx: E2BSandboxClient, policy: SandboxPolicy) -> None:
    """Best-effort FS enforcement inside the guest.

    - /skills is always read-only after mount
    - declared readonly paths get a-w
    - writable paths get u+w
    """
    for path in policy.filesystem.readonly:
        if path in (DEFAULT_SKILLS_ROOT, "/skills") or path.startswith("/skills"):
            _chmod_tree(sbx, path, "a-w")
    for path in policy.filesystem.writable:
        _chmod_tree(sbx, path, "u+w")


def _chmod_tree(sbx: E2BSandboxClient, path: str, mode: str) -> None:
    quoted = shlex.quote(path)
    try:
        sbx.commands.run(f"chmod -R {mode} {quoted}")
    except Exception:  # noqa: BLE001
        logger.debug("chmod %s %s failed", mode, path)


# ----- lazy real-client factory -----


def e2b_sandbox_factory() -> E2BClientFactory:
    """Build a factory that creates real e2b.Sandbox instances."""
    from e2b import Sandbox  # lazy: optional dependency

    def _create(**kwargs: Any) -> E2BSandboxClient:
        return Sandbox.create(**kwargs)

    return _create


def e2b_connect_factory() -> Any:
    from e2b import Sandbox

    def _connect(sandbox_id: str) -> E2BSandboxClient:
        return Sandbox.connect(sandbox_id)

    return _connect


def e2b_snapshot_lookup() -> Any:
    """Return lookup(alias) -> snapshot_id | None via E2B snapshot listing."""
    from e2b import Sandbox

    def _lookup(alias: str) -> str | None:
        paginator = Sandbox.list_snapshots(name=alias, limit=1)
        items = paginator.next_items()
        if not items:
            return None
        return items[0].snapshot_id

    return _lookup


def e2b_snapshot_create() -> Any:

    def _create(sbx: E2BSandboxClient, *, name: str) -> str:
        info = sbx.create_snapshot(name=name)
        return info.snapshot_id

    return _create
