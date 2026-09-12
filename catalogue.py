"""Dashboard compilation, redaction and search independent of Home Assistant."""

from dataclasses import dataclass
import copy
import hashlib
import json
import re
from typing import Any

from . import converter as c
from .evaluator import evaluate

REDACTED = "[REDACTED_ENTITY]"
SAFE_ATTRIBUTES = ("unit_of_measurement", "device_class", "state_class", "temperature", "current_temperature", "humidity", "brightness", "percentage", "current_position", "hvac_action")

FIELDS = ("record_type", "section", "name", "entity_id", "capability", "condition", "labels", "service", "target", "text")


class Redactor:
    def __init__(self, blocked: list[str]):
        self.blocked = frozenset(blocked)
        self.pattern = re.compile(r"(?<![a-zA-Z0-9_])(?:" + "|".join(re.escape(x) for x in sorted(blocked, key=len, reverse=True)) + r")(?![a-zA-Z0-9_])", re.I) if blocked else None

    def text(self, value: str) -> str:
        return self.pattern.sub(REDACTED, value) if self.pattern else value

    def clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.clean(x) for x in value]
        if isinstance(value, tuple):
            return [self.clean(x) for x in value]
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                cleaned_key = self.text(str(key))
                unique_key = cleaned_key
                index = 2
                while unique_key in result:
                    unique_key = f"{cleaned_key} [{index}]"
                    index += 1
                result[unique_key] = self.clean(child)
            return result
        return value


@dataclass
class Record:
    id: str
    dashboard_id: str
    fields: dict[str, str]
    condition: c.Cond
    dependencies: frozenset[str]


def _guard_cards(value: Any) -> None:
    """Unknown card semantics are retained but cannot yield a positive decision."""
    if isinstance(value, list):
        for item in value:
            _guard_cards(item)
    elif isinstance(value, dict):
        card_type = value.get("type")
        if any(
            isinstance(x, str)
            and any(marker in x for marker in ("{{", "{%", "[[[")) 
            for key, x in value.items() 
            if not (value.get("type") == "markdown" and key == "content")
        ):
            value["_dashboard_mcp_unsupported"] = "dynamic template"
        for key, item in list(value.items()):
            if key != "_dashboard_mcp_unsupported":
                _guard_cards(item)


def dependencies(condition: c.Cond) -> frozenset[str]:
    ids = set()
    for atom in c.condition_atoms(condition):
        if atom.entity:
            ids.add(atom.entity)
        if atom.raw:
            ids.update(c.ENTITY_RE.findall(atom.raw))
    return frozenset(ids)


def compile_dashboard(dashboard_id: str, data: dict, keep_image_paths: bool = False) -> list[Record]:
    if not isinstance(data, dict) or not isinstance(data.get("views"), list):
        raise ValueError("Dashboard has no exportable views configuration.")
    data = copy.deepcopy(data)
    # Guard only card trees, not view/section type declarations.
    for view in data["views"]:
        if not isinstance(view, dict):
            continue
        _guard_cards(view.get("cards", []))
        for section in view.get("sections", []):
            if isinstance(section, dict):
                _guard_cards(section.get("cards", []))
    items, views, menu_labels = c.extract_items(data)
    _, _, effective = c.resolve_reachability(items, views)
    records = []

    def add(item, kind, entity="", capability="", text=""):
        condition = effective[item.seq]
        fields = dict(zip(FIELDS, [kind, c.location_for(item, menu_labels), item.label,
            entity, capability, c.render_condition(condition), c.instruction_labels(text),
            item.service, ",".join(item.action_target), re.sub(r"\\([\\¦])", lambda m: m[1], c.compact_text(text, keep_image_paths))]))
        # IDs encode only location/sequence, never hidden identifiers or content hashes.
        rid = f"{dashboard_id}:{item.seq}:{len(records)}"
        records.append(Record(rid, dashboard_id, fields, condition, dependencies(condition)))

    for item in items:
        if item.content or item.url:
            add(item, "information", capability="information", text=item.content or item.url)
        if item.kind in {"action", "assistant"} and not item.primary_entities:
            add(item, "action", capability="action")
        for entity in item.primary_entities:
            if not c.ENTITY_RE.fullmatch(entity) or c.is_menu_entity(entity):
                continue
            capability = "status" if item.kind == "status" else "control" if entity.split(".")[0] in c.CONTROL_DOMAINS or item.kind == "control" else "status"
            add(item, "entity", entity, capability)
    return records


def public_state(entity: str, states: dict, redactor: Redactor) -> dict:
    if entity in redactor.blocked:
        return {"entity_id": REDACTED, "status": "redacted"}
    state = states.get(entity)
    if not state:
        return {"entity_id": entity, "status": "unavailable"}
    return redactor.clean({
        "entity_id": entity,
        "status": "unavailable" if state["state"] in {"unknown", "unavailable"} else "available",
        "state": state["state"],
        "attributes": {k: v for k, v in state.get("attributes", {}).items() if k in SAFE_ATTRIBUTES},
        "last_updated": state.get("last_updated"),
    })


def render_record(record: Record, options: dict, states: dict, include_states: bool = False) -> dict:
    redactor = Redactor(options["blocked_entities"])
    fields = redactor.clean(record.fields)
    entity = record.fields["entity_id"]
    blocked = entity in redactor.blocked
    if blocked and record.fields["name"] == c.human_entity(entity):
        fields["name"] = "Redacted entity"
    result = {"id": record.id, "dashboard_id": record.dashboard_id, **fields}
    decision = evaluate(record.condition, states, options.get("viewer_user_id") or None)
    if blocked:
        status, reason = "redacted", "Entity excluded by the integration configuration."
    elif options["mode"] == "smart":
        status, reason = "agent_evaluation_required", "Evaluate the complete condition before use."
    else:
        status, reason = decision.status, decision.reason
    result["access"] = status
    result["reason"] = reason
    # Retain the record and section, while withholding gated payload in dumb mode.
    if options["mode"] == "dumb" and status in {"restricted", "undetermined"}:
        for key in ("text", "service", "target"):
            if result[key]:
                result[key] = "[RESTRICTED_CONTENT]"
    if include_states:
        if entity:
            result["live"] = public_state(entity, states, redactor) if status in {"allowed", "agent_evaluation_required", "redacted"} else {"status": "withheld"}
        if options["include_condition_states"]:
            result["condition_states"] = [public_state(e, states, redactor) for e in sorted(record.dependencies)]
    return redactor.clean(result)


def ranked_search(records: list[dict], query: str) -> list[dict]:
    """Search only the rendered/redacted records. No arbitrary regex execution."""
    query = query.casefold().strip()
    tokens = re.findall(r"[\w]+", query)
    if not tokens:
        return []
    ranked = []
    for record in records:
        name = record["name"].casefold()
        entity = record["entity_id"].casefold()
        title = " ".join((name, entity, record["section"].casefold()))
        body = " ".join(str(record[k]).casefold() for k in FIELDS)
        if not all(t in body for t in tokens):
            continue
        score = (100 if query == entity else 0) + (80 if query == name else 0) + (30 if query in title else 0) + sum(5 for t in tokens if t in title)
        ranked.append((score, record["id"], record))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [r for _, _, r in ranked]


def delimited(records: list[dict]) -> str:
    columns = (*FIELDS, "access", "reason")
    return "\n".join([c.DELIM.join(columns)] + [c.DELIM.join(c.escape_field(r[k]) for k in columns) for r in records])


def revision(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
