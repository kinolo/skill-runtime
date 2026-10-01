"""Skill mount payloads and capability → sandbox policy mapping.

Capability intersection is computed at resolve time; this module turns the
effective policy into concrete sandbox enforcement (network allowlist, FS
read-only locks, writable workspace).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from skill_runtime.models import NetworkMode, SandboxPolicy, SkillCapabilities


@dataclass
class SkillMount:
    """Skill bundle to place inside a sandbox."""

    name: str
    target_path: str
    files: dict[str, bytes | str] = field(default_factory=dict)
    readonly: bool = True


@dataclass
class E2BNetworkConfig:
    """Plain kwargs for e2b.Sandbox.create(network=..., allow_internet_access=...)."""

    allow_internet_access: bool
    network: dict[str, Any]


def network_config_from_policy(policy: SandboxPolicy) -> E2BNetworkConfig:
    """Map SandboxPolicy → E2B network controls.

    - DENY_ALL  → no egress
    - ALLOWLIST → allow only policy.network.hosts (already capability-intersected)
    - OPEN      → full internet (dev only)
    """
    mode = policy.network.mode
    hosts = sorted(set(policy.network.hosts))

    if mode is NetworkMode.DENY_ALL or (mode is NetworkMode.ALLOWLIST and not hosts):
        return E2BNetworkConfig(
            allow_internet_access=False,
            network={"deny_out": ["0.0.0.0/0"]},
        )

    if mode is NetworkMode.OPEN:
        return E2BNetworkConfig(
            allow_internet_access=True,
            network={"allow_out": ["0.0.0.0/0"]},
        )

    # ALLOWLIST with hosts
    return E2BNetworkConfig(
        allow_internet_access=True,
        network={"allow_out": hosts, "deny_out": ["0.0.0.0/0"]},
    )


def cap_to_policy(
    caps: SkillCapabilities,
    *,
    base: SandboxPolicy | None = None,
) -> SandboxPolicy:
    """Build an effective sandbox policy from intersected capabilities.

    Capability is the ceiling; `base` (request policy) may narrow further.
    """
    policy = (base or SandboxPolicy()).model_copy(deep=True)

    cap_hosts = set(caps.network)
    req_hosts = set(policy.network.hosts)
    if req_hosts:
        policy.network.hosts = sorted(req_hosts & cap_hosts)
    else:
        policy.network.hosts = sorted(cap_hosts)

    if not policy.network.hosts and policy.network.mode is NetworkMode.OPEN:
        # open mode without hosts still leaves OPEN; deny-all is explicit.
        pass
    elif not policy.network.hosts and policy.network.mode is NetworkMode.ALLOWLIST:
        policy.network.mode = NetworkMode.DENY_ALL

    policy.filesystem.readonly = sorted(
        set(policy.filesystem.readonly) | set(caps.filesystem_read) | {"/skills"}
    )
    write = set(policy.filesystem.writable) & set(caps.filesystem_write)
    write.add("/workspace")
    policy.filesystem.writable = sorted(write)

    return policy


def skill_mounts_from_files(
    name: str,
    target_path: str,
    files: dict[str, bytes | str],
    *,
    readonly: bool = True,
) -> SkillMount:
    return SkillMount(
        name=name,
        target_path=target_path,
        files=dict(files),
        readonly=readonly,
    )
