from __future__ import annotations

import sqlite3
import os
from collections.abc import Callable

Migration = Callable[[sqlite3.Connection], None]


def migration_1(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            external_subject TEXT UNIQUE,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            title TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
            content TEXT NOT NULL,
            provider_id TEXT,
            model_id TEXT,
            route_id TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            latency_ms REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS providers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            adapter_type TEXT NOT NULL,
            endpoint TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            health_status TEXT NOT NULL DEFAULT 'unknown',
            config_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS models (
            id TEXT PRIMARY KEY,
            provider_id TEXT NOT NULL REFERENCES providers(id),
            name TEXT NOT NULL,
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'unknown',
            context_window INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(provider_id, name)
        );
        CREATE TABLE IF NOT EXISTS roles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            purpose TEXT NOT NULL,
            requirements_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS routes (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            role_id TEXT NOT NULL REFERENCES roles(id),
            priority INTEGER NOT NULL DEFAULT 100,
            policy_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS route_targets (
            route_id TEXT NOT NULL REFERENCES routes(id),
            provider_id TEXT NOT NULL REFERENCES providers(id),
            model_id TEXT NOT NULL REFERENCES models(id),
            ordinal INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(route_id, provider_id, model_id)
        );
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            root_path TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS environment_targets (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            target_type TEXT NOT NULL,
            address TEXT,
            credential_ref TEXT,
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            health_status TEXT NOT NULL DEFAULT 'unknown',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tools (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL,
            input_schema_json TEXT NOT NULL,
            execution_policy_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS integrations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            integration_type TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}',
            credential_ref TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id),
            session_id TEXT REFERENCES sessions(id),
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            payload_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS execution_attempts (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id),
            tool_id TEXT,
            environment_target_id TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            started_at TEXT,
            finished_at TEXT,
            exit_code INTEGER,
            output_ref TEXT,
            error_code TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS memory_items (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id),
            project_id TEXT REFERENCES projects(id),
            namespace TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS telemetry_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT,
            event_type TEXT NOT NULL,
            stage TEXT,
            duration_ms REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS update_history (
            id TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            release_ref TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS secret_refs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            backend TEXT NOT NULL,
            backend_ref TEXT NOT NULL,
            purpose TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


def migration_2(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_session_created
            ON messages(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_status_created
            ON jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_telemetry_request_stage
            ON telemetry_events(request_id, stage);
        CREATE INDEX IF NOT EXISTS idx_memory_scope
            ON memory_items(user_id, project_id, namespace);
        INSERT INTO app_meta(key, value) VALUES ('application_name', 'VirtizAI')
            ON CONFLICT(key) DO NOTHING;
        """
    )


def migration_3(connection: sqlite3.Connection) -> None:
    """Add provider lifecycle, model capabilities, and route policy state."""
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(providers)")
    }
    if "failure_count" not in columns:
        connection.execute("ALTER TABLE providers ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0")
    if "success_count" not in columns:
        connection.execute("ALTER TABLE providers ADD COLUMN success_count INTEGER NOT NULL DEFAULT 0")
    if "failure_threshold" not in columns:
        connection.execute("ALTER TABLE providers ADD COLUMN failure_threshold INTEGER NOT NULL DEFAULT 3")
    if "recovery_threshold" not in columns:
        connection.execute("ALTER TABLE providers ADD COLUMN recovery_threshold INTEGER NOT NULL DEFAULT 2")
    if "last_health_check_at" not in columns:
        connection.execute("ALTER TABLE providers ADD COLUMN last_health_check_at TEXT")
    if "last_health_error" not in columns:
        connection.execute("ALTER TABLE providers ADD COLUMN last_health_error TEXT")

    model_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(models)")
    }
    additions = {
        "reasoning_score": "REAL",
        "coding_score": "REAL",
        "tool_use_score": "REAL",
        "structured_output": "INTEGER",
        "vision_support": "INTEGER",
        "expected_latency_ms": "REAL",
        "relative_cost": "REAL",
        "locality": "TEXT",
        "first_token_latency_ms": "REAL",
        "user_overrides_json": "TEXT NOT NULL DEFAULT '{}'",
        "last_seen_at": "TEXT",
        "last_error": "TEXT",
    }
    for name, definition in additions.items():
        if name not in model_columns:
            connection.execute(f"ALTER TABLE models ADD COLUMN {name} {definition}")

    route_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(route_targets)")
    }
    if "enabled" not in route_columns:
        connection.execute("ALTER TABLE route_targets ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
    if "conditions_json" not in route_columns:
        connection.execute("ALTER TABLE route_targets ADD COLUMN conditions_json TEXT NOT NULL DEFAULT '{}'")

    message_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(messages)")
    }
    for name, definition in {
        "usage_exact": "INTEGER",
        "ttft_ms": "REAL",
        "estimated_cost": "REAL",
    }.items():
        if name not in message_columns:
            connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")

    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_models_provider_status
            ON models(provider_id, status);
        CREATE INDEX IF NOT EXISTS idx_routes_role_enabled_priority
            ON routes(role_id, enabled, priority);
        INSERT INTO roles(id, name, purpose, requirements_json)
            VALUES
              ('role-secretary', 'secretary', 'Fast conversational secretary', '{"latency_sensitive":true}'),
              ('role-general-reasoning', 'general-reasoning', 'General reasoning', '{}'),
              ('role-coding', 'coding', 'Software development and code', '{"coding":0.7}'),
              ('role-deep-reasoning', 'deep-reasoning', 'Deep analysis and reasoning', '{"reasoning":0.8}'),
              ('role-vision', 'vision', 'Vision-capable tasks', '{"vision":true}')
            ON CONFLICT(name) DO NOTHING;
        """
    )


def migration_4(connection: sqlite3.Connection) -> None:
    """Add durable execution policy, structured results, and audit records."""
    for name, definition in {
        "driver": "TEXT",
        "result_json": "TEXT",
        "timeout": "INTEGER",
        "max_output_bytes": "INTEGER",
        "cancelled_at": "TEXT",
    }.items():
        columns = {row[1] for row in connection.execute("PRAGMA table_info(execution_attempts)")}
        if name not in columns:
            connection.execute(f"ALTER TABLE execution_attempts ADD COLUMN {name} {definition}")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS permission_profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL,
            tool_ids_json TEXT NOT NULL DEFAULT '[]',
            limits_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS execution_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            session_id TEXT,
            job_id TEXT,
            attempt_id TEXT,
            model_id TEXT,
            provider_id TEXT,
            tool_id TEXT,
            target_id TEXT,
            driver TEXT NOT NULL,
            sanitized_args_json TEXT NOT NULL DEFAULT '{}',
            authorization TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_execution_audit_job ON execution_audit(job_id, created_at);
        INSERT INTO permission_profiles(id, name, description, limits_json)
          VALUES ('profile-secretary', 'Secretary', 'Read-heavy safe tools', '{"max_concurrency":1,"timeout_seconds":10}'),
                 ('profile-general', 'General Agent', 'Approved operational tools', '{"max_concurrency":2,"timeout_seconds":60}'),
                 ('profile-coding', 'Coding Agent', 'Workspace and git tools', '{"max_concurrency":2,"timeout_seconds":300}'),
                 ('profile-admin', 'Administrative Agent', 'Powerful approved tools', '{"max_concurrency":1,"timeout_seconds":120}')
          ON CONFLICT(name) DO NOTHING;
        """
    )


def migration_5(connection: sqlite3.Connection) -> None:
    """Add canonical project/environment sources and scoped context metadata."""
    memory_columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_items)")}
    for name, definition in {
        "memory_type": "TEXT NOT NULL DEFAULT 'durable'",
        "importance": "REAL NOT NULL DEFAULT 0.5",
        "source_ref": "TEXT",
        "token_estimate": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if name not in memory_columns:
            connection.execute(f"ALTER TABLE memory_items ADD COLUMN {name} {definition}")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS project_repositories (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            repository_url TEXT,
            local_path TEXT,
            branch TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS project_context_sources (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            source_type TEXT NOT NULL,
            location TEXT NOT NULL,
            include_patterns_json TEXT NOT NULL DEFAULT '[]',
            exclude_patterns_json TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS project_environment_targets (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            environment_target_id TEXT NOT NULL REFERENCES environment_targets(id) ON DELETE CASCADE,
            relationship TEXT NOT NULL DEFAULT 'relevant',
            PRIMARY KEY(project_id, environment_target_id)
        );
        CREATE TABLE IF NOT EXISTS environment_services (
            id TEXT PRIMARY KEY,
            environment_target_id TEXT NOT NULL REFERENCES environment_targets(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            endpoint TEXT,
            protocol TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS environment_relationships (
            source_environment_id TEXT NOT NULL REFERENCES environment_targets(id) ON DELETE CASCADE,
            target_environment_id TEXT NOT NULL REFERENCES environment_targets(id) ON DELETE CASCADE,
            relationship TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(source_environment_id, target_environment_id, relationship)
        );
        CREATE TABLE IF NOT EXISTS context_budgets (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            max_tokens INTEGER NOT NULL,
            memory_tokens INTEGER NOT NULL DEFAULT 0,
            project_tokens INTEGER NOT NULL DEFAULT 0,
            environment_tokens INTEGER NOT NULL DEFAULT 0,
            tool_tokens INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO context_budgets(id, name, max_tokens, memory_tokens, project_tokens, environment_tokens, tool_tokens)
          VALUES ('budget-secretary', 'secretary', 2000, 500, 500, 500, 0),
                 ('budget-agent', 'agent', 16000, 4000, 6000, 4000, 2000)
          ON CONFLICT(name) DO NOTHING;
        CREATE INDEX IF NOT EXISTS idx_memory_namespace_importance
          ON memory_items(namespace, importance DESC, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_context_sources_project
          ON project_context_sources(project_id, enabled);
        """
    )


def migration_6(connection: sqlite3.Connection) -> None:
    """Add context composition telemetry and memory lifecycle fields."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_items)")}
    for name, definition in {"confidence": "REAL NOT NULL DEFAULT 0.5", "superseded_by": "TEXT", "expires_at": "TEXT", "verified_state": "TEXT NOT NULL DEFAULT 'unverified'"}.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE memory_items ADD COLUMN {name} {definition}")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS context_builds (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            session_id TEXT,
            project_id TEXT,
            budget_name TEXT NOT NULL,
            task_summary TEXT,
            category_tokens_json TEXT NOT NULL DEFAULT '{}',
            selected_sources_json TEXT NOT NULL DEFAULT '[]',
            omitted_sources_json TEXT NOT NULL DEFAULT '[]',
            total_tokens INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_context_builds_session ON context_builds(session_id, created_at);
        """
    )


def migration_7(connection: sqlite3.Connection) -> None:
    """Add explicit interface identity/session mappings and Discord preferences."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS interface_identities (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            interface_type TEXT NOT NULL,
            external_subject TEXT NOT NULL,
            display_name TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(interface_type, external_subject)
        );
        CREATE TABLE IF NOT EXISTS interface_sessions (
            interface_type TEXT NOT NULL,
            external_session_key TEXT NOT NULL,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(interface_type, external_session_key)
        );
        CREATE TABLE IF NOT EXISTS interface_preferences (
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            interface_type TEXT NOT NULL,
            response_verbosity TEXT,
            execution_updates TEXT,
            tool_details TEXT,
            PRIMARY KEY(user_id, interface_type)
        );
        CREATE TABLE IF NOT EXISTS discord_config (
            id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            bot_secret_ref TEXT,
            mode TEXT NOT NULL DEFAULT 'existing_bot',
            allow_dms INTEGER NOT NULL DEFAULT 1,
            require_mentions INTEGER NOT NULL DEFAULT 1,
            slash_commands INTEGER NOT NULL DEFAULT 1,
            dedicated_channel_id TEXT,
            release_channel_id TEXT,
            allowed_servers_json TEXT NOT NULL DEFAULT '[]',
            allowed_channels_json TEXT NOT NULL DEFAULT '[]',
            allowed_users_json TEXT NOT NULL DEFAULT '[]',
            allowed_roles_json TEXT NOT NULL DEFAULT '[]',
            admin_users_json TEXT NOT NULL DEFAULT '[]',
            admin_roles_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS interface_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            interface_type TEXT NOT NULL,
            user_id TEXT,
            session_id TEXT,
            event_type TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO discord_config(id) VALUES ('discord-default') ON CONFLICT(id) DO NOTHING;
        """
    )


def migration_8(connection: sqlite3.Connection) -> None:
    """Add communication policy, cost profiles, and telemetry retention settings."""
    message_columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
    for name, definition in {"cached_tokens": "INTEGER", "generation_latency_ms": "REAL", "fallback_reason": "TEXT", "cost_source": "TEXT"}.items():
        if name not in message_columns:
            connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS communication_preferences (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            response_verbosity TEXT NOT NULL DEFAULT 'normal',
            execution_updates TEXT NOT NULL DEFAULT 'important_milestones',
            tool_details TEXT NOT NULL DEFAULT 'summary',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS request_overrides (
            request_id TEXT PRIMARY KEY,
            response_verbosity TEXT,
            execution_updates TEXT,
            tool_details TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS cost_profiles (
            id TEXT PRIMARY KEY,
            provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            model_name TEXT NOT NULL,
            input_cost_per_million REAL,
            output_cost_per_million REAL,
            currency TEXT NOT NULL DEFAULT 'USD',
            effective_at TEXT,
            source TEXT NOT NULL DEFAULT 'user',
            UNIQUE(provider_id, model_name)
        );
        CREATE TABLE IF NOT EXISTS telemetry_retention (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL UNIQUE,
            retention_days INTEGER NOT NULL
        );
        INSERT INTO telemetry_retention(id, event_type, retention_days)
          VALUES ('retention-request', 'request_stage', 30), ('retention-context', 'context_build', 30), ('retention-audit', 'execution_audit', 90)
          ON CONFLICT(event_type) DO NOTHING;
        """
    )


def migration_9(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
    for name, definition in {"cached_tokens": "INTEGER", "generation_latency_ms": "REAL", "fallback_reason": "TEXT", "cost_source": "TEXT"}.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")


def migration_10(connection: sqlite3.Connection) -> None:
    """Add release manifests, update policies, and recovery records."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS release_manifests (
            version TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            release_url TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            published_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS update_policies (
            id TEXT PRIMARY KEY,
            channel TEXT NOT NULL DEFAULT 'stable',
            version_policy TEXT NOT NULL DEFAULT 'follow_channel',
            pinned_version TEXT,
            skipped_versions_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS update_backups (
            id TEXT PRIMARY KEY,
            update_id TEXT NOT NULL REFERENCES update_history(id) ON DELETE CASCADE,
            backup_ref TEXT NOT NULL,
            checksum_sha256 TEXT,
            verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO update_policies(id) VALUES ('default') ON CONFLICT(id) DO NOTHING;
        """
    )


def migration_12(connection: sqlite3.Connection) -> None:
    """Persist Discord thread/session relationships for restart-safe conversations."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS discord_thread_sessions (
            thread_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            guild_id TEXT,
            parent_channel_id TEXT NOT NULL,
            starter_message_id TEXT,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_discord_thread_sessions_session
            ON discord_thread_sessions(session_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_updated
            ON sessions(updated_at DESC);
        """
    )


def migration_11(connection: sqlite3.Connection) -> None:
    """Synthetic schema-transition fixture: schema-only state is incompatible with v10."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_transition_proof (
            id TEXT PRIMARY KEY,
            transformed_value TEXT NOT NULL
        );
        UPDATE app_meta SET value='schema-11-transformed' WHERE key='synthetic_transition';
        INSERT OR IGNORE INTO schema_transition_proof(id, transformed_value)
          VALUES ('synthetic-transition', 'schema-11-only');
        """
    )


def migration_13(connection: sqlite3.Connection) -> None:
    """Persist generic operational state transitions and Discord alert routing."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(discord_config)")}
    if "alert_channel_id" not in columns:
        connection.execute("ALTER TABLE discord_config ADD COLUMN alert_channel_id TEXT")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS operational_events (
            id TEXT PRIMARY KEY,
            component_type TEXT NOT NULL,
            component_id TEXT NOT NULL,
            component_name TEXT NOT NULL,
            previous_state TEXT,
            new_state TEXT NOT NULL,
            reason TEXT,
            severity TEXT NOT NULL DEFAULT 'info',
            initial_state INTEGER NOT NULL DEFAULT 0,
            notification_status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_operational_events_component
            ON operational_events(component_type, component_id, created_at);
    """)


def migration_14(connection: sqlite3.Connection) -> None:
    """Persist execution identity and routing metadata on each message."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
    if "metadata_json" not in columns:
        connection.execute("ALTER TABLE messages ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")


MIGRATIONS: tuple[tuple[int, Migration], ...] = (
    (1, migration_1),
    (2, migration_2),
    (3, migration_3),
    (4, migration_4),
    (5, migration_5),
    (6, migration_6),
    (7, migration_7),
    (8, migration_8),
    (9, migration_9),
    (10, migration_10),
    (11, migration_11),
    (12, migration_12),
    (13, migration_13),
    (14, migration_14),
)


def apply_migrations(connection: sqlite3.Connection) -> int:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    current = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()[0]
    ceiling = int(os.environ.get("VIRTIZAI_SCHEMA_CEILING", "2147483647"))
    for version, migration in MIGRATIONS:
        if version > ceiling:
            break
        if version <= current:
            continue
        migration(connection)
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (version,)
        )
        current = version
    connection.commit()
    return current
