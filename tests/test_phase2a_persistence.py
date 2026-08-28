import json

from virtizai_core.db import Database
from virtizai_core.migrations import MIGRATIONS
from virtizai_core.project_assignments import ProjectAssignmentService


EXPECTED_MANAGERS = {
    "pm-sarah": "Sarah",
    "pm-michael": "Michael",
    "pm-emily": "Emily",
    "pm-daniel": "Daniel",
    "pm-rachel": "Rachel",
}


def test_migration_20_creates_and_seeds_project_manager_persistence(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setenv("VIRTIZAI_SCHEMA_CEILING", "19")
    database = Database(path)
    database.open()
    assert database.fetch_one("SELECT MAX(version) FROM schema_migrations")[0] == 19
    database.close()

    # This test deliberately isolates migration 20 rather than asserting the
    # repository's current schema version.
    monkeypatch.setenv("VIRTIZAI_SCHEMA_CEILING", "20")
    database = Database(path)
    database.open()

    assert database.fetch_one("SELECT MAX(version) FROM schema_migrations")[0] == 20
    managers = database.fetch_all(
        "SELECT id, name, role_id FROM project_managers ORDER BY id"
    )
    assert {row["id"]: row["name"] for row in managers} == EXPECTED_MANAGERS
    assert {row["role_id"] for row in managers} == {"role-project-lead"}
    assert {
        "assigned_project_manager_id",
        "project_manager_assigned_at",
    } <= {row["name"] for row in database.fetch_all("PRAGMA table_info(projects)")}
    assert {
        "project_managers",
        "project_assignments",
        "project_assignment_audit",
        "project_plans",
    } <= {
        row["name"]
        for row in database.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
    }
    database.close()


def test_assignment_audit_and_structured_plan_survive_reopen(tmp_path):
    path = tmp_path / "state.db"
    database = Database(path)
    database.open()
    database.execute(
        "INSERT INTO projects(id,name,assigned_project_manager_id,project_manager_assigned_at) "
        "VALUES('project-1','Persistent project','pm-sarah','2026-08-27T12:00:00Z')"
    )
    database.execute(
        "INSERT INTO project_assignments"
        "(id,project_id,project_manager_id,source_interface,created_at,updated_at) "
        "VALUES('assignment-1','project-1','pm-sarah','test','2026-08-27T12:00:00Z','2026-08-27T12:00:00Z')"
    )
    database.execute(
        "INSERT INTO project_assignment_audit(id,assignment_id,action,metadata_json) "
        "VALUES('audit-1','assignment-1','assigned','{\"reason\":\"initial\"}')"
    )
    plan = {
        "summary": "Persist Phase 2A",
        "milestones": [
            {
                "title": "Foundation",
                "objective": "Persist PM state",
                "acceptance_criteria": ["State survives reopen"],
            }
        ],
    }
    database.execute(
        "INSERT INTO project_plans(id,project_id,assignment_id,plan_json) VALUES(?,?,?,?)",
        ("plan-1", "project-1", "assignment-1", json.dumps(plan, sort_keys=True)),
    )
    database.close()

    reopened = Database(path)
    reopened.open()
    project = reopened.fetch_one("SELECT * FROM projects WHERE id='project-1'")
    assignment = reopened.fetch_one(
        "SELECT * FROM project_assignments WHERE id='assignment-1'"
    )
    audit = reopened.fetch_one(
        "SELECT * FROM project_assignment_audit WHERE id='audit-1'"
    )
    persisted_plan = reopened.fetch_one("SELECT * FROM project_plans WHERE id='plan-1'")

    assert project["assigned_project_manager_id"] == "pm-sarah"
    assert project["project_manager_assigned_at"] == "2026-08-27T12:00:00Z"
    assert assignment["project_manager_id"] == "pm-sarah"
    assert assignment["status"] == "assigned"
    assert audit["action"] == "assigned"
    assert json.loads(audit["metadata_json"]) == {"reason": "initial"}
    assert persisted_plan["version"] == 1
    assert persisted_plan["status"] == "proposed"
    assert json.loads(persisted_plan["plan_json"]) == plan
    assert reopened.fetch_one("SELECT COUNT(*) FROM schema_migrations")[0] == len(MIGRATIONS)
    reopened.close()


def _project(database, project_id):
    database.execute(
        "INSERT INTO projects(id,name) VALUES(?,?)", (project_id, project_id)
    )


def test_assignment_is_idempotent_and_persists_audit_and_project_columns(tmp_path):
    database = Database(tmp_path / "assignment.db")
    database.open()
    _project(database, "p1")
    service = ProjectAssignmentService(database)

    first = service.assign("p1", actor_user_id=None, source_interface="cli")
    second = service.assign(
        "p1", actor_user_id=None, source_interface="webui",
        preferred_project_manager_id="pm-rachel",
    )

    assert second == first
    assert database.fetch_one("SELECT COUNT(*) FROM project_assignments")[0] == 1
    assert database.fetch_one("SELECT COUNT(*) FROM project_assignment_audit")[0] == 1
    project = database.fetch_one("SELECT * FROM projects WHERE id='p1'")
    assert project["assigned_project_manager_id"] == first.project_manager_id
    assert project["project_manager_assigned_at"] is not None
    database.close()


def test_assignment_distributes_by_active_load_and_excludes_disabled(tmp_path):
    database = Database(tmp_path / "distribution.db")
    database.open()
    database.execute("UPDATE project_managers SET enabled=0 WHERE id='pm-daniel'")
    service = ProjectAssignmentService(database)
    selected = []
    for number in range(8):
        project_id = f"p{number}"
        _project(database, project_id)
        selected.append(service.assign(
            project_id, actor_user_id=None, source_interface="test"
        ).project_manager_id)

    assert "pm-daniel" not in selected
    counts = {manager: selected.count(manager) for manager in set(selected)}
    assert max(counts.values()) - min(counts.values()) <= 1
    assert selected[:4] == ["pm-emily", "pm-michael", "pm-rachel", "pm-sarah"]
    database.close()


def test_assignment_survives_reopen_and_has_no_execution_identity(tmp_path):
    path = tmp_path / "reopen.db"
    database = Database(path)
    database.open()
    _project(database, "persistent")
    expected = ProjectAssignmentService(database).assign(
        "persistent", actor_user_id=None, source_interface="cli"
    )
    database.close()

    reopened = Database(path)
    reopened.open()
    actual = ProjectAssignmentService(reopened).assign(
        "persistent", actor_user_id=None, source_interface="discord"
    )
    assert actual == expected
    assert not hasattr(actual, "provider_id")
    assert not hasattr(actual, "model_id")
    reopened.close()
