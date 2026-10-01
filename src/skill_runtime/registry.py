"""Skill package registry types. Concrete storage lives in adapters."""

from skill_runtime.adapters.memory import InMemorySkillRegistry
from skill_runtime.models import SkillManifest

__all__ = ["InMemorySkillRegistry", "SkillManifest"]
