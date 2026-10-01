"""Capability intersection + skill registry resolution."""

from skill_runtime.adapters.memory import InMemorySkillRegistry
from skill_runtime.models import SkillCapabilities, SkillManifest, SkillRef, intersect_capabilities


def _cap(net, r, w, tools):
    return SkillCapabilities(
        network=net, filesystem_read=r, filesystem_write=w, tools=tools
    )


def test_intersection_narrows_to_common():
    a = _cap(["api.a.com", "shared.com"], ["/workspace", "/data"], ["/workspace"], ["bash", "python"])
    b = _cap(["api.b.com", "shared.com"], ["/workspace"], ["/workspace"], ["python"])
    out = intersect_capabilities([a, b])
    assert out.network == ["shared.com"]
    assert out.filesystem_read == ["/workspace"]
    assert out.filesystem_write == ["/workspace"]
    assert out.tools == ["python"]


def test_intersection_empty_lists():
    out = intersect_capabilities([])
    assert out.network == []
    assert out.tools == []
    assert out.filesystem_read == ["/workspace"]


def test_registry_applies_intersection_to_each_manifest():
    reg = InMemorySkillRegistry(
        [
            SkillManifest(
                name="pdf",
                version="1.0.0",
                capabilities=_cap(["a.com", "b.com"], ["/workspace"], ["/workspace"], ["bash", "python"]),
            ),
            SkillManifest(
                name="docx",
                version="1.0.0",
                capabilities=_cap(["b.com", "c.com"], ["/workspace"], ["/workspace"], ["python"]),
            ),
        ]
    )
    resolved = reg.resolve([SkillRef(name="pdf"), SkillRef(name="docx")])
    assert len(resolved) == 2
    for r in resolved:
        assert r.manifest.capabilities.network == ["b.com"]
        assert r.manifest.capabilities.tools == ["python"]


def test_resolve_version_pin_and_missing():
    reg = InMemorySkillRegistry(
        [
            SkillManifest(name="pdf", version="1.0.0"),
            SkillManifest(name="pdf", version="2.0.0"),
        ]
    )
    pinned = reg.resolve([SkillRef(name="pdf", version="1.0.0")])[0]
    assert pinned.manifest.version == "1.0.0"
    latest = reg.resolve([SkillRef(name="pdf", version="*")])[0]
    assert latest.manifest.version == "2.0.0"
    try:
        reg.resolve([SkillRef(name="nope")])
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
