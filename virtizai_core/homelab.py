"""Sanitized import and read-only lookup for trusted homelab facts."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .db import Database


_SECRET_FIELD = re.compile(
    r"(?:pass(?:word|phrase)?|secret|token|api[ _-]?key|private[ _-]?key|credential|"
    r"auth(?:orization)?|cookie|session[ _-]?(?:key|token)|client[ _-]?secret)",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^#{2,6}\s+(.+?)\s*$")
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+")
_FACT = re.compile(r"^\s*(?:[-*]\s+)?\*{0,2}([^:|]+?)\*{0,2}\s*:\s*(.*?)\s*$")
_TABLE_ROW = re.compile(r"^\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|?\s*$")
_TABLE_RULE = re.compile(r"^:?-{3,}:?$")
_CODE_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_CURRENT_STATE = re.compile(
    r"^\s*current\s+state\s*:\s*(?:the\s+)?(?:active\s+)?"
    r"(?:(?:product\s*/\s*)?platform\s*(?:is|:)\s*)?"
    r"(?P<platform>[^();]{1,100}?)\s*\((?P<endpoint>[^()]{1,200})\)\s*"
    r"(?:is\s+active\s*)?[,;\N{EM DASH}\N{EN DASH}-]+\s*"
    r"(?:underlying\s+)?os\s*(?:is|:)\s*(?P<os>[^;]{1,160}?)\s*;\s*"
    r"(?:product\s+)?version\s*(?:is|:)\s*(?P<version>[^;]{1,100}?)[.]?\s*$",
    re.IGNORECASE,
)
_UNLABELLED_ACTIVE_STATE = re.compile(
    r"^\s*(?P<platform>[^();]{1,100}?)\s*\((?P<endpoint>[^()]{1,200})\)\s*"
    r"remains\s+active\s+on\s+(?P<os>[^/;]{1,160}?)\s*/\s*"
    r"(?P<version>[^/;]{1,100}?)[.]?\s*$",
    re.IGNORECASE,
)


def _stable_id(kind: str, *parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "virtizai:homelab:" + kind + ":" + "\x1f".join(parts)))


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().strip("`*_"))


def _safe_field(name: str, value: str) -> bool:
    if _SECRET_FIELD.search(name):
        return False
    # Refuse common inline secret assignments even under an innocent-looking key.
    return not bool(re.search(r"\b(?:password|token|secret|api[_-]?key)\s*[=:]", value, re.I))


def _heading_aliases(canonical: str) -> set[str]:
    """Return meaningful heading segments, without inventing word-level aliases."""
    aliases = {canonical}
    for part in re.split(
        r"\s*(?:/|&|\band\b|[\N{EM DASH}\N{EN DASH}])\s*|\s*[()]\s*",
        canonical,
        flags=re.I,
    ):
        part = _clean(part)
        if part and not _SECRET_FIELD.search(part):
            aliases.add(part)
    return aliases


def _identity_aliases(name: str, value: str) -> set[str]:
    """Extract only normalized hostnames/IPs from explicitly identity-like fields."""
    field_words = set(re.findall(r"[a-z]+", name.casefold()))
    hostname_field = bool(field_words.intersection({"hostname", "fqdn", "endpoint"})) or field_words in (
        {"host"}, {"host", "name"}
    )
    if not hostname_field and not field_words.intersection({"ip", "address"}):
        return set()
    aliases: set[str] = set()
    for item in re.split(r"\s*(?:/|,|;)\s*|\s+", value):
        candidate = _clean(
            re.sub(r"^(?:hostname|host|fqdn|ip|address|endpoint)\s*[:=]\s*", "", item, flags=re.I)
        ).strip("[]()").rstrip(".")
        try:
            aliases.add(str(ipaddress.ip_address(candidate)))
        except ValueError:
            if hostname_field and re.fullmatch(
                r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", candidate
            ):
                aliases.add(candidate.casefold())
    return aliases


def _coalesced_entities(
    records: list[tuple[str, str, str, int]],
) -> tuple[list[str], dict[str, str]]:
    """Connect sections only when they share a validated hostname or IP."""
    headings = sorted({record[0] for record in records}, key=lambda item: (item.casefold(), item))
    parent = {heading: heading for heading in headings}

    def find(heading: str) -> str:
        while parent[heading] != heading:
            parent[heading] = parent[parent[heading]]
            heading = parent[heading]
        return heading

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        canonical = min((left_root, right_root), key=lambda item: (item.casefold(), item))
        parent[left_root if canonical == right_root else right_root] = canonical

    identity_owner: dict[str, str] = {}
    for heading, name, value, _ in records:
        for identity in _identity_aliases(name, value):
            normalized = identity.casefold()
            owner = identity_owner.setdefault(normalized, heading)
            union(owner, heading)

    heading_to_canonical = {heading: find(heading) for heading in headings}
    canonicals = sorted(set(heading_to_canonical.values()), key=lambda item: (item.casefold(), item))
    return canonicals, heading_to_canonical


def _current_state_facts(line: str) -> list[tuple[str, str]]:
    """Parse one bounded labelled or strictly shaped active-state sentence."""
    if len(line) > 700:
        return []
    sentence = re.split(r"(?<=[.!?])\s+(?=[A-Z])", line, maxsplit=1)[0]
    match = _CURRENT_STATE.match(sentence) or _UNLABELLED_ACTIVE_STATE.match(sentence)
    if not match:
        return []
    return [
        ("Product / platform", _clean(match.group("platform"))),
        ("Hostname / IP", _clean(match.group("endpoint"))),
        ("Underlying OS", _clean(match.group("os"))),
        ("Product version", _clean(match.group("version"))),
    ]


def _active_state_paragraphs(text: str) -> list[tuple[str, str, int, int]]:
    """Return bounded plain-prose paragraphs, sourced to their first line."""
    current: str | None = None
    paragraph: list[str] = []
    first_line = 0
    last_line = 0
    paragraphs: list[tuple[str, str, int, int]] = []
    in_code_fence = False

    def flush() -> None:
        nonlocal paragraph, first_line, last_line
        if current and paragraph:
            joined = " ".join(part.strip() for part in paragraph)
            if len(joined) <= 700:
                paragraphs.append((current, joined, first_line, last_line))
        paragraph = []
        first_line = 0
        last_line = 0

    for line_number, line in enumerate(text.splitlines(), 1):
        fence = _CODE_FENCE.match(line)
        if fence:
            flush()
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        heading = _HEADING.match(line)
        if _MARKDOWN_HEADING.match(line):
            flush()
            current = _clean(heading.group(1)) if heading else None
            continue
        if not line.strip() or "|" in line or _LIST_ITEM.match(line):
            flush()
            continue
        if current:
            if not paragraph:
                first_line = line_number
            paragraph.append(line)
            last_line = line_number
    flush()
    return paragraphs


@dataclass(frozen=True)
class ImportSummary:
    entities: int
    aliases: int
    facts: int
    excluded_fields: int
    source: str = "homelab.md"


@dataclass(frozen=True)
class PropertyResolution:
    status: str
    canonical: str | None = None
    matches: tuple[str, ...] = ()


def _normalize_property(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", _clean(value).casefold()))


def resolve_property(requested: str, canonical_properties: list[str]) -> PropertyResolution:
    """Resolve only exact labels and the bounded trusted-fact vocabulary."""
    requested = _clean(requested)
    ordered = tuple(sorted(set(canonical_properties), key=lambda item: (item.casefold(), item)))
    exact = tuple(item for item in ordered if item == requested)
    if exact:
        return PropertyResolution("found", exact[0], exact)
    normalized = _normalize_property(requested)
    normalized_matches = tuple(item for item in ordered if _normalize_property(item) == normalized)
    if len(normalized_matches) == 1:
        return PropertyResolution("found", normalized_matches[0], normalized_matches)
    if len(normalized_matches) > 1:
        return PropertyResolution("ambiguous", matches=normalized_matches)
    semantic_targets = {
        "os": "underlying os", "operating system": "underlying os",
        "version": "product version", "ip": "hostname ip",
        "ip address": "hostname ip", "hostname": "hostname ip",
        "host name": "hostname ip", "platform": "product platform",
        "product": "product platform",
    }
    target = semantic_targets.get(normalized)
    if target is None:
        return PropertyResolution("not_found")
    semantic_matches = tuple(item for item in ordered if _normalize_property(item) == target)
    if len(semantic_matches) == 1:
        return PropertyResolution("found", semantic_matches[0], semantic_matches)
    if len(semantic_matches) > 1:
        return PropertyResolution("ambiguous", matches=semantic_matches)
    return PropertyResolution("not_found")


class HomelabImporter:
    """Import only a named homelab.md file; no sibling file is inspected."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def import_file(self, path: str | Path) -> ImportSummary:
        source = Path(path)
        if source.name.casefold() != "homelab.md":
            raise ValueError("homelab importer accepts only homelab.md")
        if source.is_symlink() or source.resolve().name.casefold() != "homelab.md":
            raise ValueError("homelab.md must be a direct, non-symlink file")
        # Reading this one explicit path is deliberate: do not enumerate the parent.
        text = source.read_text(encoding="utf-8")
        return self.import_text(text)

    def import_text(self, text: str) -> ImportSummary:
        current: str | None = None
        records: list[tuple[str, str, str, int]] = []
        excluded = 0
        active_state_lines: set[int] = set()
        for heading, paragraph, line_number, last_line in _active_state_paragraphs(text):
            current_state = _current_state_facts(paragraph)
            if current_state:
                active_state_lines.update(range(line_number, last_line + 1))
            for name, value in current_state:
                if not _safe_field(name, value):
                    excluded += 1
                    continue
                records.append((heading, name, value, line_number))
        for line_number, line in enumerate(text.splitlines(), 1):
            if line_number in active_state_lines:
                continue
            heading = _HEADING.match(line)
            if heading:
                current = _clean(heading.group(1))
                continue
            fact = _FACT.match(line)
            if current and fact:
                name, value = _clean(fact.group(1)), _clean(fact.group(2))
                if not name or not value:
                    continue
                if not _safe_field(name, value):
                    excluded += 1
                    continue
                records.append((current, name, value, line_number))
                continue
            table = _TABLE_ROW.match(line)
            if current and table:
                name, value = _clean(table.group(1)), _clean(table.group(2))
                if (name.casefold(), value.casefold()) == ("field", "value") or (
                    _TABLE_RULE.fullmatch(name) and _TABLE_RULE.fullmatch(value)
                ):
                    continue
                if not name or not value:
                    continue
                if not _safe_field(name, value):
                    excluded += 1
                    continue
                records.append((current, name, value, line_number))
                continue

        entity_names, heading_to_canonical = _coalesced_entities(records)
        aliases = 0
        facts = 0
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        desired_fact_ids = {
            _stable_id(
                "fact",
                _stable_id("entity", heading_to_canonical[canonical].casefold()),
                name.casefold(),
                value,
                _stable_id("provenance", "homelab.md", str(line_number), digest),
            )
            for canonical, name, value, line_number in records
            if name.casefold() not in {"alias", "aliases"}
        }
        with self.database.transaction():
            old_entity_ids = [row["entity_id"] for row in self.database.fetch_all(
                "SELECT DISTINCT f.entity_id FROM homelab_facts f "
                "JOIN homelab_provenance p ON p.id=f.provenance_id WHERE p.source_name=?",
                ("homelab.md",),
            )]
            if desired_fact_ids:
                placeholders = ",".join("?" for _ in desired_fact_ids)
                self.database.execute(
                    "DELETE FROM homelab_facts WHERE provenance_id IN "
                    "(SELECT id FROM homelab_provenance WHERE source_name=?) "
                    f"AND id NOT IN ({placeholders})",
                    ("homelab.md", *sorted(desired_fact_ids)),
                )
            else:
                self.database.execute(
                    "DELETE FROM homelab_facts WHERE provenance_id IN "
                    "(SELECT id FROM homelab_provenance WHERE source_name=?)",
                    ("homelab.md",),
                )
            if old_entity_ids:
                placeholders = ",".join("?" for _ in old_entity_ids)
                self.database.execute(
                    f"DELETE FROM homelab_aliases WHERE entity_id IN ({placeholders})",
                    tuple(old_entity_ids),
                )
            for canonical in entity_names:
                entity_id = _stable_id("entity", canonical.casefold())
                self.database.execute(
                    "INSERT INTO homelab_entities(id,canonical_name) VALUES(?,?) "
                    "ON CONFLICT(id) DO UPDATE SET canonical_name=excluded.canonical_name, updated_at=CURRENT_TIMESTAMP",
                    (entity_id, canonical),
                )
                generated_aliases: set[str] = set()
                grouped_headings = {
                    heading for heading, mapped in heading_to_canonical.items() if mapped == canonical
                }
                for heading in grouped_headings:
                    generated_aliases.update(_heading_aliases(heading))
                for record_heading, name, value, _ in records:
                    if record_heading in grouped_headings:
                        generated_aliases.update(_identity_aliases(name, value))
                for alias in sorted(generated_aliases, key=str.casefold):
                    self.database.execute(
                        "INSERT INTO homelab_aliases(alias,entity_id) VALUES(?,?) "
                        "ON CONFLICT(alias) DO UPDATE SET entity_id=excluded.entity_id",
                        (alias, entity_id),
                    )
                    aliases += 1
            for canonical, name, value, line_number in records:
                entity_id = _stable_id("entity", heading_to_canonical[canonical].casefold())
                if name.casefold() in {"alias", "aliases"}:
                    for alias in (_clean(item) for item in value.split(",")):
                        if alias and not _SECRET_FIELD.search(alias):
                            self.database.execute(
                                "INSERT INTO homelab_aliases(alias,entity_id) VALUES(?,?) "
                                "ON CONFLICT(alias) DO UPDATE SET entity_id=excluded.entity_id",
                                (alias, entity_id),
                            )
                            aliases += 1
                    continue
                provenance_id = _stable_id("provenance", "homelab.md", str(line_number), digest)
                self.database.execute(
                    "INSERT OR IGNORE INTO homelab_provenance(id,source_name,source_line,source_digest) VALUES(?,?,?,?)",
                    (provenance_id, "homelab.md", line_number, digest),
                )
                fact_id = _stable_id("fact", entity_id, name.casefold(), value, provenance_id)
                self.database.execute(
                    "INSERT INTO homelab_facts(id,entity_id,property,value,provenance_id,verified_at) "
                    "VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(id) DO UPDATE SET "
                    "value=excluded.value, verified_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP",
                    (fact_id, entity_id, name, value, provenance_id),
                )
                facts += 1
            self.database.execute(
                "DELETE FROM homelab_provenance WHERE source_name=? "
                "AND id NOT IN (SELECT provenance_id FROM homelab_facts)",
                ("homelab.md",),
            )
            if old_entity_ids:
                placeholders = ",".join("?" for _ in old_entity_ids)
                self.database.execute(
                    f"DELETE FROM homelab_entities WHERE id IN ({placeholders}) "
                    "AND id NOT IN (SELECT entity_id FROM homelab_facts)",
                    tuple(old_entity_ids),
                )
        return ImportSummary(len(entity_names), aliases, facts, excluded)


class HomelabFacts:
    """Generic read-only alias/property lookup with bounded evidence."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def requested_property(content: str, canonical_properties: list[str]) -> str | None:
        """Extract a bounded property phrase; resolution remains in ``lookup``."""
        normalized = _normalize_property(content)
        candidates = list(canonical_properties) + [
            "operating system", "ip address", "host name", "os", "version",
            "ip", "hostname", "platform", "product",
        ]
        for candidate in sorted(set(candidates), key=lambda item: (-len(_normalize_property(item)), item)):
            phrase = _normalize_property(candidate)
            if re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", normalized):
                return candidate
        match = re.search(r"\bwhat\s+(?:is\s+)?(?:the\s+)?([a-z0-9-]+)\b", normalized)
        candidate = match.group(1) if match else None
        return None if candidate in {None, "my", "our", "it", "that", "this"} else candidate

    def lookup(
        self, alias: str, property: str | None = None, session_id: str | None = None
    ) -> dict:
        term = _clean(alias)
        session_entity = None
        if session_id and term.casefold() in {"it", "that", "this", "that one", "this one"}:
            session_entity = self.same_session_entity(session_id)
            if session_entity:
                term = session_entity["canonical_name"]
        if not term or _SECRET_FIELD.search(term) or (property and _SECRET_FIELD.search(property)):
            return {"status": "not_found", "query": term, "entities": []}
        rows = self.database.fetch_all(
            "SELECT DISTINCT e.id,e.canonical_name,e.entity_type,e.status FROM homelab_entities e "
            "JOIN homelab_aliases a ON a.entity_id=e.id "
            "WHERE a.alias = ? COLLATE NOCASE ORDER BY e.canonical_name LIMIT 8",
            (term,),
        )
        if not rows:
            return {"status": "not_found", "query": term, "entities": []}
        entities = []
        for row in rows:
            parameters: tuple = (row["id"],)
            property_clause = ""
            if property:
                canonical_properties = [item["property"] for item in self.database.fetch_all(
                    "SELECT DISTINCT property FROM homelab_facts WHERE entity_id=? ORDER BY property",
                    (row["id"],),
                )]
                property_resolution = resolve_property(property, canonical_properties)
                if property_resolution.status == "ambiguous":
                    return {
                        "status": "ambiguous_property", "query": term, "property": property,
                        "property_matches": list(property_resolution.matches), "entities": [],
                    }
                if property_resolution.status == "not_found":
                    entities.append({
                        "id": row["id"], "name": row["canonical_name"], "type": row["entity_type"],
                        "status": row["status"], "facts": [],
                    })
                    continue
                property_clause = " AND f.property = ? COLLATE NOCASE"
                parameters += (property_resolution.canonical,)
            fact_rows = self.database.fetch_all(
                "SELECT f.property,f.value,f.status,p.source_name,p.source_line,p.source_digest "
                "FROM homelab_facts f JOIN homelab_provenance p ON p.id=f.provenance_id "
                "WHERE f.entity_id=?" + property_clause +
                " ORDER BY f.property,f.updated_at DESC LIMIT 50",
                parameters,
            )
            entities.append({
                "id": row["id"], "name": row["canonical_name"], "type": row["entity_type"],
                "status": row["status"],
                "facts": [{
                    "property": fact["property"], "value": fact["value"], "status": fact["status"],
                    "evidence": {"source": fact["source_name"], "line": fact["source_line"], "digest": fact["source_digest"]},
                } for fact in fact_rows],
            })
        status = "ambiguous" if len(entities) > 1 else ("property_not_found" if property and not entities[0]["facts"] else "found")
        return {
            "status": status, "query": term, "property": property,
            "context_scope": "same_session" if session_entity else "explicit_alias",
            "entities": entities,
        }

    def same_session_entity(self, session_id: str) -> dict | None:
        """Recover only a prior successful lookup marker from this exact session."""
        rows = self.database.fetch_all(
            "SELECT metadata_json FROM messages WHERE session_id=? AND role='assistant' "
            "ORDER BY created_at DESC,rowid DESC LIMIT 12", (session_id,),
        )
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            marker = metadata.get("homelab_lookup") if isinstance(metadata, dict) else None
            if not isinstance(marker, dict) or marker.get("status") != "found":
                continue
            entity_id = marker.get("entity_id")
            if not isinstance(entity_id, str):
                continue
            entity = self.database.fetch_one(
                "SELECT id,canonical_name,entity_type,status FROM homelab_entities WHERE id=?", (entity_id,),
            )
            return dict(entity) if entity else None
        return None
