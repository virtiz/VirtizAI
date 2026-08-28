from virtizai_core.work_intake import WorkIntakeClassifier


def test_generic_work_intents_have_structured_metadata():
    classifier = WorkIntakeClassifier()
    cases = {
        "Hello, how are you?": "conversation",
        "remove old Discord threads": "operational",
        "inspect the container cluster": "infrastructure",
        "edit the parser module": "coding",
        "research and compare storage approaches": "research",
        "manage a release roadmap with milestones": "project",
    }
    for request, expected in cases.items():
        result = classifier.classify(request)
        assert result.intent == expected
        assert result.complexity in {"low", "medium", "high"}
        assert result.tier in {"automatic", "local", "cloud"}
        assert result.risk in {"low", "medium", "high"}
        assert isinstance(result.tools, tuple)
        assert isinstance(result.followup, bool)
        assert result.metadata()["needs_pm"] is (expected == "project")
        assert result.needs_project_manager is (expected == "project")


def test_explicit_overrides_are_preserved_without_execution_identity():
    classifier = WorkIntakeClassifier()
    coding = classifier.classify("/local /coding inspect the repository")
    project = classifier.classify("/cloud /project plan the release")
    assert (coding.intent, coding.tier, coding.reason) == (
        "coding", "local", "explicit override"
    )
    assert (project.intent, project.tier, project.needs_project_manager) == (
        "project", "cloud", True
    )
    assert not hasattr(project, "provider_id")
    assert not hasattr(project, "model_id")


def test_homelab_lookup_preserves_education_restart_and_research_intents():
    classifier = WorkIntakeClassifier()
    assert classifier.classify("what is the hostname of my NAS?").intent == "homelab_lookup"
    assert classifier.classify("explain what NAS storage means").intent == "conversation"
    assert classifier.classify("restart my NAS server").intent == "infrastructure"
    assert classifier.classify("research and compare NAS storage").intent == "research"
    assert classifier.classify("What is OPNsense?").intent == "conversation"
    assert classifier.classify("restart my router infrastructure").intent == "infrastructure"
    restart_followup = classifier.classify("restart it")
    assert restart_followup.intent == "infrastructure"
    assert restart_followup.intent != "homelab_lookup"
    assert classifier.classify("what version is it running?").intent == "homelab_lookup"
    assert classifier.classify("which address is recorded for my gateway?").intent == "homelab_lookup"


def test_structural_multi_stage_work_requires_project_management():
    classifier = WorkIntakeClassifier()
    project_requests = (
        "Plan an upgrade of my Nextcloud environment including backup, validation, and rollback. Don't execute anything yet.",
        "Investigate the failure, fix the cause, and verify the repair.",
        "Implement the parser changes and add tests.",
    )
    for request in project_requests:
        intake = classifier.classify(request)
        assert intake.intent == "project", request
        assert intake.needs_project_manager is True, request
        assert intake.complexity == "high", request


def test_structural_project_detection_preserves_adjacent_intents():
    classifier = WorkIntakeClassifier()
    assert classifier.classify("research and compare backup approaches").intent == "research"
    assert classifier.classify("explain upgrade rollback strategies").intent == "conversation"
    assert classifier.classify("restart my backup server").intent == "infrastructure"
    assert classifier.classify("what version is recorded for my server?").intent == "homelab_lookup"
