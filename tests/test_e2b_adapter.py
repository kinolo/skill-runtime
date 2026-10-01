"""E2B adapter tests with a fake client (no network, no API key)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from skill_runtime.adapters.e2b import (
    E2BEnvironment,
    E2BSandbox,
    layer_key,
    skill_fingerprint,
    snapshot_alias,
)
from skill_runtime.models import SkillCapabilities
from skill_runtime.policy import SkillMount, cap_to_policy


@dataclass
class FakeFS:
    files: dict[str, bytes] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    writes: list[tuple[str, Any]] = field(default_factory=list)

    def make_dir(self, path: str) -> bool:
        self.dirs.add(path)
        return True

    def write(self, path: str, data: Any) -> Any:
        raw = data if isinstance(data, bytes) else str(data).encode()
        self.files[path] = raw
        self.writes.append((path, raw))
        return {"path": path}


@dataclass
class FakeCmds:
    runs: list[str] = field(default_factory=list)

    def run(self, cmd: str, timeout: int | None = None, **kwargs: Any) -> Any:
        self.runs.append(cmd)

        class R:
            exit_code = 0
            stdout = "ok"
            stderr = ""

        return R()


@dataclass
class FakeSbx:
    sandbox_id: str
    filesystem: FakeFS
    commands: FakeCmds
    killed: bool = False
    paused: bool = False
    snapshots: list[str] = field(default_factory=list)

    def kill(self) -> bool:
        self.killed = True
        return True

    def pause(self, keep_memory: bool | None = None) -> bool:
        self.paused = True
        return True

    def create_snapshot(self, name: str | None = None) -> Any:
        self.snapshots.append(name or "anon")

        class Info:
            snapshot_id = f"snap_{name}" if name else "snap_anon"

        return Info()


def make_factory(store: list[FakeSbx]):
    def _create(**kwargs: Any) -> FakeSbx:
        sbx = FakeSbx(
            sandbox_id=f"sbx_{len(store)+1}",
            filesystem=FakeFS(),
            commands=FakeCmds(),
        )
        sbx.create_kwargs = kwargs  # type: ignore[attr-defined]
        store.append(sbx)
        return sbx

    return _create


def _mounts() -> list[SkillMount]:
    return [
        SkillMount(
            name="pdf",
            target_path="/skills/pdf",
            files={"SKILL.md": "# pdf\n", "scripts/x.py": "print(1)\n"},
            readonly=True,
        ),
        SkillMount(
            name="docx",
            target_path="/skills/docx",
            files={"SKILL.md": "# docx\n"},
            readonly=True,
        ),
    ]


def test_skill_fingerprint_changes_with_content():
    a = [SkillMount(name="pdf", target_path="/skills/pdf", files={"a": "1"})]
    b = [SkillMount(name="pdf", target_path="/skills/pdf", files={"a": "2"})]
    c = [SkillMount(name="pdf", target_path="/skills/pdf", files={"a": "1"})]
    assert skill_fingerprint(a) != skill_fingerprint(b)
    assert skill_fingerprint(a) == skill_fingerprint(c)


def test_layer_key_includes_base_packages_skills():
    mounts = _mounts()
    k1 = layer_key("python:3.12", ["curl"], mounts)
    k2 = layer_key("python:3.12", ["curl", "git"], mounts)
    k3 = layer_key("python:3.12", ["curl"], mounts[:1])
    assert len({k1, k2, k3}) == 3
    assert snapshot_alias(k1).startswith("sr-env-")


def test_sandbox_create_boots_from_snapshot_and_mounts_readonly():
    store: list[FakeSbx] = []
    adapter = E2BSandbox(create_sandbox=make_factory(store))

    caps = SkillCapabilities(
        network=["api.example.com"],
        filesystem_read=["/workspace"],
        filesystem_write=["/workspace"],
        tools=["python"],
    )
    policy = cap_to_policy(caps)
    mounts = _mounts()

    sid = adapter.create("snap_sr-env-abc", policy, mounts)
    assert sid == "sbx_1"
    sbx = store[0]

    # boot from snapshot/template
    assert sbx.create_kwargs["template"] == "snap_sr-env-abc"
    # capability network enforced
    assert sbx.create_kwargs["allow_internet_access"] is True
    assert sbx.create_kwargs["network"]["allow_out"] == ["api.example.com"]

    # skill files written
    assert "/skills/pdf/SKILL.md" in sbx.filesystem.files
    assert "/skills/pdf/scripts/x.py" in sbx.filesystem.files
    assert "/skills/docx/SKILL.md" in sbx.filesystem.files

    # read-only lock + workspace
    chmods = [c for c in sbx.commands.runs if c.startswith("chmod")]
    assert any("a-w" in c and "/skills/pdf" in c for c in chmods)
    assert any("/workspace" in c for c in chmods)
    assert "/workspace" in sbx.filesystem.dirs

    handle = adapter.get(sid)
    assert handle.env_ref == "snap_sr-env-abc"
    assert len(handle.mounts) == 2


def test_sandbox_deny_all_when_capability_network_empty():
    store: list[FakeSbx] = []
    adapter = E2BSandbox(create_sandbox=make_factory(store))
    caps = SkillCapabilities(
        network=[],
        filesystem_read=["/workspace"],
        filesystem_write=["/workspace"],
        tools=["python"],
    )
    policy = cap_to_policy(caps)
    adapter.create("snap_x", policy, [])
    assert store[0].create_kwargs["allow_internet_access"] is False
    assert store[0].create_kwargs["network"]["deny_out"] == ["0.0.0.0/0"]


def test_environment_bakes_snapshot_once_then_hits_cache():
    store: list[FakeSbx] = []
    baked: list[str] = []

    def create_sandbox(**kwargs: Any) -> FakeSbx:
        return make_factory(store)(**kwargs)

    def snapshot_create(sbx: FakeSbx, *, name: str) -> str:
        baked.append(name)
        return f"snap_{name}"

    env = E2BEnvironment(
        create_sandbox=create_sandbox,
        snapshot_create=snapshot_create,
    )
    mounts = _mounts()
    ref1 = env.prepare_mounts(
        base_image="python:3.12",
        packages=["curl", "pip:requests"],
        mounts=mounts,
        snapshot_policy="prefer_cache",
    )
    ref2 = env.prepare_mounts(
        base_image="python:3.12",
        packages=["curl", "pip:requests"],
        mounts=mounts,
        snapshot_policy="prefer_cache",
    )
    assert ref1 == ref2
    assert env.build_count == 1
    assert env.hit_count == 1
    assert len(baked) == 1
    assert ref1.startswith("snap_sr-env-")

    # bake sandbox was killed after snapshot
    assert store[0].killed is True
    # packages installed
    assert any("apt-get install" in c and "curl" in c for c in store[0].commands.runs)
    assert any("pip install" in c and "requests" in c for c in store[0].commands.runs)
    # skills written read-only into the layer
    assert "/skills/pdf/SKILL.md" in store[0].filesystem.files


def test_environment_always_build_busts_cache():
    store: list[FakeSbx] = []
    env = E2BEnvironment(
        create_sandbox=lambda **kw: make_factory(store)(**kw),
        snapshot_create=lambda sbx, *, name: f"snap_{name}",
    )
    mounts = _mounts()
    env.prepare_mounts(
        base_image="b", packages=[], mounts=mounts, snapshot_policy="prefer_cache"
    )
    env.prepare_mounts(
        base_image="b", packages=[], mounts=mounts, snapshot_policy="always_build"
    )
    assert env.build_count == 2


def test_environment_pinned_snapshot_short_circuits():
    env = E2BEnvironment()
    ref = env.prepare_mounts(
        base_image="b",
        packages=[],
        mounts=[],
        snapshot_policy="pin",
        pinned_snapshot_id="snap_pinned",
    )
    assert ref == "snap_pinned"
    assert env.build_count == 0


def test_end_to_end_capability_intersection_on_sandbox():
    """Two skills with different caps → intersection drives network + FS."""
    store: list[FakeSbx] = []
    adapter = E2BSandbox(create_sandbox=make_factory(store))

    from skill_runtime.models import intersect_capabilities

    a = SkillCapabilities(
        network=["x.com", "y.com"],
        filesystem_read=["/workspace", "/data"],
        filesystem_write=["/workspace", "/tmp"],
        tools=["python"],
    )
    b = SkillCapabilities(
        network=["y.com", "z.com"],
        filesystem_read=["/workspace"],
        filesystem_write=["/workspace"],
        tools=["python"],
    )
    policy = cap_to_policy(intersect_capabilities([a, b]))
    adapter.create("snap_env", policy, _mounts())

    kw = store[0].create_kwargs
    assert kw["network"]["allow_out"] == ["y.com"]
    chmods = " ".join(store[0].commands.runs)
    assert "/skills" in chmods or "/skills/pdf" in chmods
    assert "/workspace" in chmods
