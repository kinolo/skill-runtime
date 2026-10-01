"""Capability → sandbox policy mapping and E2B network config."""

from skill_runtime.models import (
    NetworkMode,
    SandboxPolicy,
    SkillCapabilities,
    intersect_capabilities,
)
from skill_runtime.policy import cap_to_policy, network_config_from_policy


def test_allowlist_maps_to_allow_out_hosts():
    policy = SandboxPolicy()
    policy.network.mode = NetworkMode.ALLOWLIST
    policy.network.hosts = ["api.a.com", "api.b.com"]
    net = network_config_from_policy(policy)
    assert net.allow_internet_access is True
    assert net.network["allow_out"] == ["api.a.com", "api.b.com"]
    assert net.network["deny_out"] == ["0.0.0.0/0"]


def test_empty_allowlist_becomes_deny_all():
    policy = SandboxPolicy()
    policy.network.mode = NetworkMode.ALLOWLIST
    policy.network.hosts = []
    net = network_config_from_policy(policy)
    assert net.allow_internet_access is False
    assert net.network["deny_out"] == ["0.0.0.0/0"]


def test_deny_all_and_open():
    p = SandboxPolicy()
    p.network.mode = NetworkMode.DENY_ALL
    assert network_config_from_policy(p).allow_internet_access is False

    p.network.mode = NetworkMode.OPEN
    net = network_config_from_policy(p)
    assert net.allow_internet_access is True
    assert net.network["allow_out"] == ["0.0.0.0/0"]


def test_cap_to_policy_narrows_hosts_and_fs():
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
    caps = intersect_capabilities([a, b])
    policy = cap_to_policy(caps)

    assert policy.network.hosts == ["y.com"]
    assert "/skills" in policy.filesystem.readonly
    assert "/workspace" in policy.filesystem.writable
    assert "/tmp" not in policy.filesystem.writable


def test_cap_to_policy_respects_request_narrowing():
    caps = SkillCapabilities(
        network=["a.com", "b.com"],
        filesystem_read=["/workspace"],
        filesystem_write=["/workspace"],
        tools=["python"],
    )
    base = SandboxPolicy()
    base.network.hosts = ["a.com"]
    policy = cap_to_policy(caps, base=base)
    assert policy.network.hosts == ["a.com"]


def test_empty_capability_network_forces_deny_all():
    caps = SkillCapabilities(
        network=[],
        filesystem_read=["/workspace"],
        filesystem_write=["/workspace"],
        tools=["python"],
    )
    policy = cap_to_policy(caps)
    assert policy.network.mode is NetworkMode.DENY_ALL
    net = network_config_from_policy(policy)
    assert net.allow_internet_access is False
