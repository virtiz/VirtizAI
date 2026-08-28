"""Durable, provider-independent project-manager assignment selection."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectAssignment:
    id: str
    project_id: str
    project_manager_id: str
    project_manager_name: str
    status: str


class ProjectAssignmentService:
    """Return a project's assignment, or persist the least-loaded enabled PM."""

    ACTIVE_STATUSES = ("assigned", "planned")

    def __init__(self, database) -> None:
        self.database = database

    @staticmethod
    def _result(row) -> ProjectAssignment:
        return ProjectAssignment(
            id=row["id"], project_id=row["project_id"],
            project_manager_id=row["project_manager_id"],
            project_manager_name=row["project_manager_name"], status=row["status"],
        )

    def assign(
        self,
        project_id: str,
        *,
        actor_user_id: str | None,
        source_interface: str,
        preferred_project_manager_id: str | None = None,
    ) -> ProjectAssignment:
        existing = self.database.fetch_one(
            """SELECT a.id,a.project_id,a.project_manager_id,a.status,
                      pm.name AS project_manager_name
               FROM project_assignments a
               JOIN project_managers pm ON pm.id=a.project_manager_id
               WHERE a.project_id=?""",
            (project_id,),
        )
        if existing is not None:
            return self._result(existing)

        project = self.database.fetch_one("SELECT id FROM projects WHERE id=?", (project_id,))
        if project is None:
            raise LookupError("Project not found")
        if preferred_project_manager_id:
            manager = self.database.fetch_one(
                "SELECT id,name FROM project_managers WHERE id=? AND enabled=1",
                (preferred_project_manager_id,),
            )
            if manager is None:
                raise LookupError("Selected Project Manager is unavailable")
        else:
            manager = self.database.fetch_one(
                """SELECT pm.id,pm.name,COUNT(a.id) AS active_assignments
                   FROM project_managers pm
                   LEFT JOIN project_assignments a
                     ON a.project_manager_id=pm.id
                    AND a.status IN ('assigned','planned')
                   WHERE pm.enabled=1
                   GROUP BY pm.id,pm.name
                   ORDER BY active_assignments ASC, pm.id ASC
                   LIMIT 1"""
            )
            if manager is None:
                raise LookupError("No enabled Project Manager is available")

        assignment_id = str(uuid.uuid4())
        self.database.execute(
            """INSERT INTO project_assignments
               (id,project_id,project_manager_id,assigned_by_user_id,source_interface)
               VALUES(?,?,?,?,?)""",
            (assignment_id, project_id, manager["id"], actor_user_id, source_interface),
        )
        self.database.execute(
            """UPDATE projects
               SET assigned_project_manager_id=?,
                   project_manager_assigned_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (manager["id"], project_id),
        )
        self.database.execute(
            """INSERT INTO project_assignment_audit
               (id,assignment_id,action,actor_user_id,metadata_json)
               VALUES(?,?,'assigned',?,?)""",
            (str(uuid.uuid4()), assignment_id, actor_user_id,
             json.dumps({"source_interface": source_interface}, sort_keys=True)),
        )
        return ProjectAssignment(
            assignment_id, project_id, manager["id"], manager["name"], "assigned"
        )
