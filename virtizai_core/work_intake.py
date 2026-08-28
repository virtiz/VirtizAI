"""Generic, deterministic work-intake classification."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkIntake:
    intent: str
    complexity: str
    needs_pm: bool
    tier: str
    risk: str
    tools: tuple[str, ...]
    followup: bool
    reason: str

    @property
    def needs_project_manager(self) -> bool:
        return self.needs_pm

    def metadata(self) -> dict:
        return {
            "complexity": self.complexity,
            "needs_pm": self.needs_pm,
            "tier": self.tier,
            "risk": self.risk,
            "tools": list(self.tools),
            "followup": self.followup,
        }


class WorkIntakeClassifier:
    """Classify broad action and domain signals without request-shaped rules."""

    INTENTS = {"conversation", "operational", "infrastructure", "coding", "research", "project", "homelab_lookup"}
    ACTIONS = {"create", "change", "edit", "fix", "implement", "build", "refactor", "run", "deploy", "restart", "start", "stop", "inspect", "list", "show", "find", "compare", "analyze", "research", "investigate", "plan", "manage"}
    DOMAINS = {
        "coding": {"code", "source", "repository", "repo", "file", "module", "function", "class", "test", "tests", "workspace"},
        "infrastructure": {"infrastructure", "vm", "vms", "container", "containers", "host", "hosts", "cluster", "service", "services", "deployment", "server"},
        "operational": {"cleanup", "delete", "remove", "prune", "notification", "thread", "threads", "discord", "account", "configuration"},
        "research": {"research", "investigate", "compare", "evaluate", "evidence", "sources", "paper", "papers", "study", "studies"},
        "project": {"project", "milestone", "milestones", "workflow", "release", "roadmap", "end-to-end"},
    }

    @staticmethod
    def _tokens(content: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", content.lower()))

    @staticmethod
    def _structural_project(tokens: set[str]) -> bool:
        """Recognize multi-stage delivery shapes without product-specific terms."""
        planning = bool(tokens & {"plan", "planning", "roadmap"})
        lifecycle_stages = (
            {"upgrade", "upgrading", "migration", "migrate", "migrating", "implementation", "implement"},
            {"backup", "backups", "restore", "recovery"},
            {"validation", "validate", "verification", "verify", "testing", "tests"},
            {"rollback", "revert", "backout", "cutover", "deployment", "deploy"},
        )
        lifecycle_count = sum(bool(tokens & stage) for stage in lifecycle_stages)
        investigation = bool(tokens & {"investigate", "investigation", "inspect", "analyze", "diagnose", "diagnosis"})
        remediation = bool(tokens & {"fix", "repair", "remediate", "remediation", "patch"})
        implementation = bool(tokens & {"implement", "implementation", "build", "develop", "development"})
        verification = bool(tokens & {"validation", "validate", "verification", "verify", "testing", "test", "tests"})
        return (
            (planning and lifecycle_count >= 2)
            or (investigation and remediation and verification)
            or (implementation and verification)
        )

    def classify(self, content: str) -> WorkIntake:
        raw = content.strip()
        lowered = raw.lower()
        tier = "local" if lowered.startswith("/local ") else "cloud" if lowered.startswith("/cloud ") else "automatic"
        if tier != "automatic":
            raw = raw.split(None, 1)[1] if len(raw.split(None, 1)) == 2 else ""
            lowered = raw.lower()
        explicit = None
        for prefix, intent in (("/project ", "project"), ("/coding ", "coding")):
            if lowered.startswith(prefix):
                explicit = intent
                raw = raw[len(prefix):]
                break
        tokens = self._tokens(raw)
        actions = tokens & self.ACTIONS
        question_terms = {"what", "which", "where", "who", "show", "list", "tell"}
        educational = bool(tokens & {"explain", "teach", "learn", "education", "tutorial", "meaning", "concept"})
        restart = bool(tokens & {"restart", "start", "stop", "reboot"})
        researching = bool(tokens & self.DOMAINS["research"])
        # Classify the shape of a configured-system fact question, not particular
        # products or properties.  Product education ("what is OPNsense?") has
        # neither ownership/configuration context nor a property/entity relation.
        configured_context = bool(tokens & {"my", "our", "configured", "recorded", "persisted", "documented"})
        contextual_pronoun = bool(tokens & {"it", "that", "this"})
        relational_question = bool(re.search(
            r"\b(?:what|which)\b.+\b(?:of|for|on|does|do)\b.+|"
            r"\b(?:where|who)\s+(?:is|are)\b.+",
            raw.lower(),
        ))
        homelab_lookup = bool(tokens & question_terms) and (
            configured_context or relational_question or contextual_pronoun
        )
        scores = {name: len(tokens & vocabulary) for name, vocabulary in self.DOMAINS.items()}
        structural_project = self._structural_project(tokens)
        intent = explicit
        if intent is None:
            ranked = sorted(scores, key=lambda name: (-scores[name], name))
            intent = ranked[0] if scores[ranked[0]] else "conversation"
            multi_domain = sum(score > 0 for score in scores.values()) >= 2
            if scores["project"] or structural_project or (multi_domain and len(actions) >= 2):
                intent = "project"
            if homelab_lookup and not educational and not restart and not researching:
                intent = "homelab_lookup"
            # An action against an owned or contextual system remains an
            # infrastructure request.  Context may identify the target, but it
            # must never turn the action into a read-only facts lookup.
            if restart and (configured_context or contextual_pronoun):
                intent = "infrastructure"
        complexity = "high" if intent == "project" else "medium" if intent in {"coding", "research", "infrastructure"} else "low"
        mutating = bool(actions & {"create", "change", "edit", "fix", "implement", "build", "refactor", "deploy", "restart", "start", "stop", "delete", "remove", "prune"})
        risk = "high" if mutating and intent in {"infrastructure", "operational"} else "medium" if mutating else "low"
        tool_map = {"coding": ("workspace",), "research": ("search",), "infrastructure": ("infrastructure",), "operational": ("typed_operation",), "project": ("planning",), "homelab_lookup": ("homelab_lookup",)}
        followup = not raw.strip() or (intent != "conversation" and not actions)
        return WorkIntake(intent, complexity, intent == "project", tier, risk, tool_map.get(intent, ()), followup, "explicit override" if explicit else "generic action/domain classification")
