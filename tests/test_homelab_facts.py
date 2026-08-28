from pathlib import Path

import pytest

from virtizai_core.db import Database
from virtizai_core.homelab import HomelabFacts, HomelabImporter, resolve_property
from virtizai_core.interfaces import InterfaceRequest, InterfaceService
from virtizai_core.services import SecretaryResponse, SessionService


@pytest.fixture
def database(tmp_path: Path):
    database = Database(tmp_path / "facts.db")
    database.open()
    yield database
    database.close()


@pytest.fixture
def homelab_file(tmp_path: Path) -> Path:
    path = tmp_path / "homelab.md"
    path.write_text(
        """# Homelab\n\n## Atlas\n- aliases: nas, fileserver\n- hostname: atlas.lan\n- role: storage\n- password: fixture-must-not-be-imported\n- notes: password=also-must-not-be-imported\n\n## Edge\n- aliases: router\n- address: 192.0.2.1\n- os: OPNsense\n- version: 26.1-fixture\n\n## Relay\n- aliases: gateway\n- address: 192.0.2.2\n""",
        encoding="utf-8",
    )
    (tmp_path / "credentials.md").write_text(
        "## Never read me\n- token: credential-fixture\n", encoding="utf-8"
    )
    return path


def test_fixture_import_is_idempotent_and_sanitized(database, homelab_file):
    importer = HomelabImporter(database)
    first = importer.import_file(homelab_file)
    importer.import_file(homelab_file)

    assert (first.entities, first.facts, first.excluded_fields) == (3, 6, 2)
    assert database.fetch_one("SELECT count(*) n FROM homelab_entities")["n"] == 3
    assert database.fetch_one("SELECT count(*) n FROM homelab_facts")["n"] == 6
    assert database.fetch_one(
        "SELECT count(*) n FROM homelab_facts WHERE verified_at IS NOT NULL"
    )["n"] == 6
    stored = " ".join(
        row["value"] for row in database.fetch_all("SELECT value FROM homelab_facts")
    )
    assert "fixture-must-not-be-imported" not in stored
    assert "credential-fixture" not in stored


def test_table_and_current_state_import_router_like_facts_with_derived_aliases(database):
    text = """# Site inventory

## Router / Internet Gateway
| Field | Value |
| --- | --- |
| Role | perimeter routing |
| API token | must-not-be-imported |

Current state: active platform is pfSense Plus (edge-fw.home.arpa / 198.51.100.1) — underlying OS is FreeBSD 15.0; product version is 26.03.
"""

    summary = HomelabImporter(database).import_text(text)

    assert (summary.entities, summary.facts, summary.excluded_fields) == (1, 5, 1)
    facts = HomelabFacts(database)
    for alias in ("Router", "Internet Gateway", "edge-fw.home.arpa"):
        assert facts.lookup(alias)["status"] == "found"
        assert facts.lookup(alias)["entities"][0]["name"] == "Router / Internet Gateway"
    role = facts.lookup("Router", "Role")
    assert role["entities"][0]["facts"][0]["value"] == "perimeter routing"
    assert role["entities"][0]["facts"][0]["evidence"]["line"] == 6

    expected = {
        "Product / platform": "pfSense Plus",
        "Underlying OS": "FreeBSD 15.0",
        "Product version": "26.03",
    }
    for property_name, value in expected.items():
        result = facts.lookup("edge-fw.home.arpa", property_name)
        assert result["status"] == "found"
        assert result["entities"][0]["facts"] == [{
            "property": property_name,
            "value": value,
            "status": "asserted",
            "evidence": {
                "source": "homelab.md",
                "line": 9,
                "digest": result["entities"][0]["facts"][0]["evidence"]["digest"],
            },
        }]
    assert "must-not-be-imported" not in " ".join(
        row["value"] for row in database.fetch_all("SELECT value FROM homelab_facts")
    )


def test_em_dash_heading_resolves_left_and_full_aliases(database):
    HomelabImporter(database).import_text("## Router — OPNsense\n- role: gateway\n")

    facts = HomelabFacts(database)
    for alias in ("Router", "Router — OPNsense"):
        result = facts.lookup(alias)
        assert result["status"] == "found"
        assert result["entities"][0]["name"] == "Router — OPNsense"


def test_shared_safe_identity_coalesces_sections_without_merging_distinct_gateway(database):
    text = """# Site inventory

## Router
| Field | Value |
| --- | --- |
| Hostname / IP | edge-fw.home.arpa / 198.51.100.1 |
| Underlying OS | FreeBSD 15.0 |

## Active firewall
Current state: active platform is pfSense Plus (edge-fw.home.arpa / 198.51.100.1) — underlying OS is FreeBSD 15.0; product version is 26.03.

## Distinct gateway
| Field | Value |
| --- | --- |
| Hostname / IP | upstream.home.arpa / 198.51.100.2 |
| Role | gateway |
"""

    summary = HomelabImporter(database).import_text(text)

    assert summary.entities == 2
    router = HomelabFacts(database).lookup("router")
    assert router["status"] == "found"
    assert len(router["entities"]) == 1
    assert {(fact["property"], fact["value"]) for fact in router["entities"][0]["facts"]} >= {
        ("Underlying OS", "FreeBSD 15.0"),
        ("Product version", "26.03"),
    }
    gateway = HomelabFacts(database).lookup("Distinct gateway")
    assert gateway["status"] == "found"
    assert gateway["entities"][0]["id"] != router["entities"][0]["id"]


def test_unlabelled_active_state_coalesces_with_router_table_and_derives_identities(database):
    text = """# Site inventory

## Router
| Field | Value |
| --- | --- |
| Hostname / IP | border.example.test / 203.0.113.8 |
| Role | perimeter routing |

## Running appliance
Acme Edge (border.example.test, 203.0.113.8) remains active on ExampleOS 8.4 / 12.7.
"""

    summary = HomelabImporter(database).import_text(text)

    assert (summary.entities, summary.facts) == (1, 6)
    facts = HomelabFacts(database)
    for alias in ("Router", "border.example.test", "203.0.113.8"):
        result = facts.lookup(alias)
        assert result["status"] == "found"
        assert result["entities"][0]["name"] == "Router"
    assert {
        (fact["property"], fact["value"])
        for fact in facts.lookup("router")["entities"][0]["facts"]
    } >= {
        ("Product / platform", "Acme Edge"),
        ("Hostname / IP", "border.example.test, 203.0.113.8"),
        ("Underlying OS", "ExampleOS 8.4"),
        ("Product version", "12.7"),
    }


def test_wrapped_unlabelled_active_state_coalesces_with_router_table(database):
    text = """# Site inventory

## Router
| Field | Value |
| --- | --- |
| Hostname / IP | gateway.fixture.test / 192.0.2.44 |

## Active appliance
Fixture Gateway (gateway.fixture.test, 192.0.2.44) remains active on
FixtureOS 9.2 / 31.6.
"""

    summary = HomelabImporter(database).import_text(text)

    assert (summary.entities, summary.facts) == (1, 5)
    result = HomelabFacts(database).lookup("Router")
    assert result["status"] == "found"
    assert {(fact["property"], fact["value"]) for fact in result["entities"][0]["facts"]} >= {
        ("Underlying OS", "FixtureOS 9.2"),
        ("Product version", "31.6"),
    }
    for property_name in ("Underlying OS", "Product version"):
        evidence = HomelabFacts(database).lookup("Router", property_name)
        assert evidence["entities"][0]["facts"][0]["evidence"]["line"] == 9


def test_unlabelled_active_state_ignores_trailing_audit_sentence(database):
    text = """# Site inventory

## Active appliance
Fixture Gateway (gateway.fixture.test, 192.0.2.44) remains active on FixtureOS 9.2 / 31.6. Audit records are retained separately.
"""

    summary = HomelabImporter(database).import_text(text)

    assert (summary.entities, summary.facts) == (1, 4)
    result = HomelabFacts(database).lookup("Active appliance")
    assert {(fact["property"], fact["value"]) for fact in result["entities"][0]["facts"]} == {
        ("Product / platform", "Fixture Gateway"),
        ("Hostname / IP", "gateway.fixture.test, 192.0.2.44"),
        ("Underlying OS", "FixtureOS 9.2"),
        ("Product version", "31.6"),
    }


def test_migration_22_repairs_existing_schema_21_before_import(
    tmp_path, monkeypatch, homelab_file
):
    path = tmp_path / "schema-21.db"
    monkeypatch.setenv("VIRTIZAI_SCHEMA_CEILING", "21")
    database = Database(path)
    database.open()
    database.execute("ALTER TABLE homelab_facts DROP COLUMN verified_at")
    assert database.fetch_one("SELECT MAX(version) FROM schema_migrations")[0] == 21
    database.close()

    monkeypatch.delenv("VIRTIZAI_SCHEMA_CEILING")
    database = Database(path)
    database.open()
    result = HomelabImporter(database).import_file(homelab_file)

    assert database.fetch_one("SELECT MAX(version) FROM schema_migrations")[0] == 22
    assert "verified_at" in {
        row["name"] for row in database.fetch_all("PRAGMA table_info(homelab_facts)")
    }
    assert result.facts == 6
    assert database.fetch_one(
        "SELECT count(*) n FROM homelab_facts WHERE verified_at IS NOT NULL"
    )["n"] == 6
    database.close()


def test_importer_refuses_credentials_file(database, homelab_file):
    with pytest.raises(ValueError, match="only homelab.md"):
        HomelabImporter(database).import_file(homelab_file.with_name("credentials.md"))

    disguised = homelab_file.parent / "nested" / "homelab.md"
    disguised.parent.mkdir()
    disguised.symlink_to(homelab_file.with_name("credentials.md"))
    with pytest.raises(ValueError, match="non-symlink"):
        HomelabImporter(database).import_file(disguised)


def test_alias_property_lookup_returns_status_and_evidence(database, homelab_file):
    HomelabImporter(database).import_file(homelab_file)
    result = HomelabFacts(database).lookup("nas", "hostname")

    assert result["status"] == "found"
    assert result["entities"][0]["name"] == "Atlas"
    assert result["entities"][0]["facts"] == [{
        "property": "hostname",
        "value": "atlas.lan",
        "status": "asserted",
        "evidence": {
            "source": "homelab.md",
            "line": 5,
            "digest": result["entities"][0]["facts"][0]["evidence"]["digest"],
        },
    }]
    assert HomelabFacts(database).lookup("nas", "token")["status"] == "not_found"
    assert HomelabFacts(database).lookup("missing")["status"] == "not_found"


def test_property_resolver_is_bounded_deterministic_and_reports_ambiguity():
    properties = ["Underlying OS", "Product version", "Hostname / IP", "Product / platform"]
    expected = {
        "OS": "Underlying OS", "operating system": "Underlying OS",
        "version": "Product version", "IP": "Hostname / IP",
        "IP address": "Hostname / IP", "hostname": "Hostname / IP",
        "host name": "Hostname / IP", "platform": "Product / platform",
        "product": "Product / platform",
    }
    for requested, canonical in expected.items():
        resolution = resolve_property(requested, properties)
        assert (resolution.status, resolution.canonical) == ("found", canonical)

    assert resolve_property("kernel", properties).status == "not_found"
    collision = ["Underlying OS", "Underlying-OS"]
    assert resolve_property("Underlying OS", collision).canonical == "Underlying OS"
    ambiguous = resolve_property("underlying_os", collision)
    assert ambiguous.status == "ambiguous"
    assert ambiguous.matches == ("Underlying OS", "Underlying-OS")


def test_same_session_context_is_read_only_and_session_scoped(database, homelab_file):
    HomelabImporter(database).import_file(homelab_file)
    entity_id = database.fetch_one(
        "SELECT id FROM homelab_entities WHERE canonical_name='Atlas'"
    )["id"]
    database.execute("INSERT INTO users(id,display_name) VALUES('u','User')")
    database.execute("INSERT INTO sessions(id,user_id) VALUES('one','u'),('two','u')")
    database.execute(
        "INSERT INTO messages(id,session_id,role,content,metadata_json) VALUES(?,?,?,?,?)",
        ("m", "one", "assistant", "Atlas", '{"homelab_lookup":{"status":"found","entity_id":"' + entity_id + '"}}'),
    )
    facts = HomelabFacts(database)
    assert facts.same_session_entity("one")["canonical_name"] == "Atlas"
    assert facts.same_session_entity("two") is None
    inherited = facts.lookup("it", "hostname", session_id="one")
    assert inherited["status"] == "found"
    assert inherited["context_scope"] == "same_session"
    assert facts.lookup("it", "hostname", session_id="two")["status"] == "not_found"


def test_source_update_replaces_stale_facts_and_aliases(database, homelab_file):
    importer = HomelabImporter(database)
    importer.import_file(homelab_file)
    importer.import_text("## Atlas\n- aliases: vault\n- hostname: atlas-new.lan\n")
    importer.import_text("## Atlas\n- aliases: vault\n- hostname: atlas-new.lan\n")

    assert HomelabFacts(database).lookup("nas")["status"] == "not_found"
    current = HomelabFacts(database).lookup("vault", "hostname")
    assert current["entities"][0]["facts"][0]["value"] == "atlas-new.lan"
    assert database.fetch_one("SELECT count(*) n FROM homelab_facts")["n"] == 1
    assert database.fetch_one("SELECT count(*) n FROM homelab_provenance")["n"] == 1


@pytest.mark.asyncio
async def test_normal_interface_lookup_and_actual_same_session_followup(database, homelab_file):
    HomelabImporter(database).import_file(homelab_file)

    class CoreThatMustNotInfer:
        def __init__(self):
            self.sessions = SessionService(database)
            self.calls = 0

        async def handle_message(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("homelab lookup reached Secretary inference")

    core = CoreThatMustNotInfer()
    service = InterfaceService(database, core, delegation=None)
    session_id, first = await service.handle(
        InterfaceRequest("cli", "facts-user", "what is the hostname of my NAS?")
    )
    _, followup = await service.handle(
        InterfaceRequest("cli", "facts-user", "what is the role of it?", session_id=session_id)
    )

    assert "hostname = atlas.lan" in first.content
    assert "source: homelab.md, line 5" in first.content
    assert "role = storage" in followup.content
    assert core.calls == 0
    metadata = database.fetch_one(
        "SELECT metadata_json FROM messages WHERE id=?", (first.message_id,)
    )["metadata_json"]
    assert '"homelab_lookup"' in metadata and '"evidence"' in metadata


@pytest.mark.asyncio
async def test_router_os_and_same_session_version_use_imported_evidence(database, homelab_file):
    HomelabImporter(database).import_file(homelab_file)
    expected = {
        row["property"].casefold(): row["value"]
        for row in database.fetch_all(
            "SELECT f.property,f.value FROM homelab_facts f "
            "JOIN homelab_entities e ON e.id=f.entity_id "
            "JOIN homelab_aliases a ON a.entity_id=e.id WHERE a.alias='router'"
        )
    }

    class NoSecretary:
        def __init__(self):
            self.sessions = SessionService(database)
            self.calls = 0

        async def handle_message(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("trusted facts reached Secretary")

    core = NoSecretary()
    service = InterfaceService(database, core)
    session_id, os_response = await service.handle(
        InterfaceRequest("cli", "router-user", "what OS is my router?")
    )
    _, version_response = await service.handle(
        InterfaceRequest("cli", "router-user", "what version is it running?", session_id=session_id)
    )

    assert f"os = {expected['os']}" in os_response.content
    assert f"version = {expected['version']}" in version_response.content
    assert os_response.task_class == version_response.task_class == "homelab_lookup"
    assert core.calls == 0


@pytest.mark.asyncio
async def test_canonical_product_version_followup_persists_one_answer(database):
    HomelabImporter(database).import_text(
        "## Router\nCurrent state: active platform is Fixture Edge (router.test / 192.0.2.9) "
        "— underlying OS is FixtureOS 4; product version is 8.2.\n"
    )

    class NoSecretary:
        def __init__(self):
            self.sessions = SessionService(database)

        async def handle_message(self, *args, **kwargs):
            raise AssertionError("trusted facts reached Secretary")

    service = InterfaceService(database, NoSecretary())
    session_id, first = await service.handle(InterfaceRequest(
        "cli", "canonical-version-user", "what operating system is my router?"
    ))
    before = database.fetch_one(
        "SELECT count(*) n FROM messages WHERE session_id=? AND role='assistant'", (session_id,)
    )["n"]
    _, response = await service.handle(InterfaceRequest(
        "cli", "canonical-version-user", "what version is it running?", session_id=session_id
    ))
    answers = database.fetch_all(
        "SELECT id,content FROM messages WHERE session_id=? AND role='assistant' ORDER BY rowid",
        (session_id,),
    )

    assert "Underlying OS = FixtureOS 4" in first.content
    assert "Product version = 8.2" in response.content
    assert len(answers) == before + 1
    assert answers[-1]["id"] == response.message_id
    assert answers[-1]["content"] == response.content


@pytest.mark.asyncio
async def test_ambiguous_alias_and_missing_property_fail_groundedly(database, homelab_file):
    HomelabImporter(database).import_file(homelab_file)

    class NoSecretary:
        def __init__(self):
            self.sessions = SessionService(database)

        async def handle_message(self, *args, **kwargs):
            raise AssertionError("trusted facts reached Secretary")

    service = InterfaceService(database, NoSecretary())
    _, ambiguous = await service.handle(InterfaceRequest(
        "cli", "ambiguous-user", "what address is my router gateway?"
    ))
    _, missing = await service.handle(InterfaceRequest(
        "cli", "missing-user", "what kernel is my router running?"
    ))

    assert "multiple configured systems" in ambiguous.content
    assert "No verified data records that property" in missing.content
    assert ambiguous.task_class == missing.task_class == "homelab_lookup"


@pytest.mark.asyncio
async def test_restart_followup_does_not_inherit_lookup_or_mutation_authority(database, homelab_file):
    HomelabImporter(database).import_file(homelab_file)

    class RecordingSecretary:
        def __init__(self):
            self.sessions = SessionService(database)
            self.requests = []

        async def handle_message(self, user_id, session_id, content, *args, **kwargs):
            self.requests.append(content)
            self.sessions.add_message(session_id, "user", content)
            message_id = self.sessions.add_message(session_id, "assistant", "No action executed.")
            return SecretaryResponse(
                "request", session_id, message_id, "No action executed.",
                None, None, None, job_created=False, task_class="simple",
            )

    core = RecordingSecretary()
    service = InterfaceService(database, core)
    session_id, lookup = await service.handle(InterfaceRequest(
        "cli", "restart-user", "what OS is my router?"
    ))
    _, restart = await service.handle(InterfaceRequest(
        "cli", "restart-user", "restart it", session_id=session_id
    ))

    assert lookup.task_class == "homelab_lookup"
    assert core.requests == ["restart it"]
    assert restart.task_class != "homelab_lookup"
    assert restart.job_created is False
    metadata = database.fetch_one(
        "SELECT metadata_json FROM messages WHERE id=?", (restart.message_id,)
    )["metadata_json"]
    assert "homelab_lookup" not in metadata
