#!/usr/bin/env python3
"""Convert a Home Assistant Lovelace YAML dashboard into a readable
broken-bar-delimited database for a conversation agent.

The output is a single complete catalogue. It is not profile-filtered or
redacted. Every entity, information item, and standalone action carries its
complete effective condition after view navigation, menu selection, section,
card, and nested visibility conditions have been resolved.

Output columns are separated by the broken-bar character: ¦
Embedded line breaks are represented by: ↵
A literal field delimiter is escaped as: \¦

Output schema:
  record_type¦section¦name¦entity_id¦capability¦condition¦labels¦service¦target¦text

Readable values are used throughout. Conditions use full entity IDs with
AND/OR/NOT-style expressions; no condition aliases, path IDs, or one-letter
flags are emitted.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    import yaml
except ImportError:
    print("PyYAML is required: python3 -m pip install PyYAML", file=sys.stderr)
    raise SystemExit(2)

DELIM = "¦"
NEWLINE_MARK = "↵"

ENTITY_RE = re.compile(r"\b[a-z_][a-z0-9_]*\.[a-z0-9_]+\b", re.I)
SIMPLE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/+-]+$")
MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]*)\)")
HTML_HREF_RE = re.compile(r"<a\b[^>]*\bhref=(['\"])(.*?)\1[^>]*>(.*?)</a>", re.I | re.S)
QR_TAG_RE = re.compile(r"<ha-qr-code\b[^>]*\bdata=(['\"])(.*?)\1[^>]*>(?:\s*</ha-qr-code>)?", re.I | re.S)
IMG_TAG_RE = re.compile(r"<img\b([^>]*)>", re.I | re.S)
ATTR_RE = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(['\"])(.*?)\2", re.S)
KNOWN_HTML_TAG_RE = re.compile(r"</?(?:ha-qr-code|summary)\b[^>]*>", re.I | re.S)
SPACE_RE = re.compile(r"[ \t\f\v]+")
BLANK_RE = re.compile(r"\n\s*\n+")

CONTROL_DOMAINS = {
    "alarm_control_panel", "button", "climate", "cover", "fan", "humidifier",
    "input_boolean", "input_button", "input_datetime", "input_number", "input_text",
    "input_select", "light", "lock", "media_player", "number", "remote",
    "scene", "script", "select", "siren", "switch", "text", "vacuum",
    "water_heater",
}
STATUS_DOMAINS = {
    "binary_sensor", "calendar", "camera", "device_tracker", "person", "sensor",
    "sun", "weather", "update",
}
CONTAINER_TYPES = {"grid", "vertical-stack", "horizontal-stack"}
MENU_ENTITY_RE = re.compile(r"(?:_menu|_search)$", re.I)

# ---------- Condition AST ----------

@dataclass(frozen=True)
class Atom:
    kind: str
    entity: str = ""
    op: str = ""
    value: str = ""
    users: tuple[str, ...] = ()
    raw: str = ""


@dataclass(frozen=True)
class Cond:
    op: str
    args: tuple[Any, ...] = ()


TRUE = Cond("true")
FALSE = Cond("false")


def atom_condition(atom: Atom) -> Cond:
    return Cond("atom", (atom,))


def cond_key(c: Cond) -> str:
    if c.op in {"true", "false"}:
        return c.op
    if c.op == "atom":
        return repr(c.args[0])
    return c.op + "(" + ",".join(cond_key(x) for x in c.args) + ")"


def _branch_contradictory(branch: frozenset[Atom]) -> bool:
    eq: dict[str, str] = {}
    neq: defaultdict[str, set[str]] = defaultdict(set)
    for a in branch:
        if a.kind != "state":
            continue
        if a.op == "=":
            if a.entity in eq and eq[a.entity] != a.value:
                return True
            eq[a.entity] = a.value
            if a.value in neq[a.entity]:
                return True
        elif a.op == "!=":
            neq[a.entity].add(a.value)
            if eq.get(a.entity) == a.value:
                return True
    return False


def _dnf(c: Cond, limit: int = 8192) -> set[frozenset[Atom]]:
    if c.op == "true":
        return {frozenset()}
    if c.op == "false":
        return set()
    if c.op == "atom":
        return {frozenset((c.args[0],))}
    if c.op == "or":
        result: set[frozenset[Atom]] = set()
        for child in c.args:
            result |= _dnf(child, limit)
            if len(result) > limit:
                raise RuntimeError("condition expansion exceeded safe limit")
        return _simplify_branches(result)
    if c.op == "and":
        result: set[frozenset[Atom]] = {frozenset()}
        for child in c.args:
            child_branches = _dnf(child, limit)
            combined: set[frozenset[Atom]] = set()
            for left in result:
                for right in child_branches:
                    branch = left | right
                    if not _branch_contradictory(branch):
                        combined.add(branch)
                    if len(combined) > limit:
                        raise RuntimeError("condition expansion exceeded safe limit")
            result = _simplify_branches(combined)
            if not result:
                return set()
        return result
    raise RuntimeError(f"unknown condition operator {c.op}")


def _simplify_branches(branches: set[frozenset[Atom]]) -> set[frozenset[Atom]]:
    branches = {b for b in branches if not _branch_contradictory(b)}
    ordered = sorted(branches, key=lambda b: (len(b), sorted(repr(a) for a in b)))
    kept: list[frozenset[Atom]] = []
    for branch in ordered:
        if any(existing <= branch for existing in kept):
            continue
        kept.append(branch)
    return set(kept)


def _from_dnf(branches: set[frozenset[Atom]]) -> Cond:
    branches = _simplify_branches(branches)
    if not branches:
        return FALSE
    if frozenset() in branches:
        return TRUE
    terms: list[Cond] = []
    for branch in sorted(branches, key=lambda b: (len(b), sorted(repr(a) for a in b))):
        atoms = tuple(Cond("atom", (a,)) for a in sorted(branch, key=repr))
        terms.append(atoms[0] if len(atoms) == 1 else Cond("and", atoms))
    return terms[0] if len(terms) == 1 else Cond("or", tuple(terms))


def mk_and(*conditions: Cond) -> Cond:
    result: set[frozenset[Atom]] = {frozenset()}
    for condition in conditions:
        if condition.op == "true":
            continue
        if condition.op == "false":
            return FALSE
        child = _dnf(condition)
        combined: set[frozenset[Atom]] = set()
        for left in result:
            for right in child:
                branch = left | right
                if not _branch_contradictory(branch):
                    combined.add(branch)
                    if len(combined) > 8192:
                        raise RuntimeError("condition expansion exceeded safe limit")
        result = _simplify_branches(combined)
        if not result:
            return FALSE
    return _from_dnf(result)


def mk_or(*conditions: Cond) -> Cond:
    result: set[frozenset[Atom]] = set()
    for condition in conditions:
        if condition.op == "true":
            return TRUE
        if condition.op == "false":
            continue
        result |= _dnf(condition)
        result = _simplify_branches(result)
    return _from_dnf(result)

def normalise_condition(raw: Any) -> Cond:
    if raw is None:
        return TRUE
    if isinstance(raw, list):
        return mk_and(*(normalise_condition(x) for x in raw))
    if not isinstance(raw, dict):
        return atom_condition(Atom("raw", raw=min_json(raw)))
    ctype = raw.get("condition")
    keys_by_type = {
        "state": {"condition", "entity", "state", "state_not"},
        "numeric_state": {"condition", "entity", "above", "below"},
        "user": {"condition", "users"},
        "and": {"condition", "conditions"},
        "or": {"condition", "conditions"},
    }
    if ctype in keys_by_type and set(raw) - keys_by_type[ctype]:
        return atom_condition(Atom("raw", raw=min_json(raw)))
    if "attribute" in raw or ctype == "not":
        return atom_condition(Atom("raw", raw=min_json(raw)))
    if ctype == "and":
        return mk_and(*(normalise_condition(x) for x in raw.get("conditions", [])))
    if ctype == "or":
        return mk_or(*(normalise_condition(x) for x in raw.get("conditions", [])))
    if ctype == "state":
        entity = str(raw.get("entity", ""))
        result: list[Cond] = []
        if "state" in raw:
            states = raw["state"] if isinstance(raw["state"], list) else [raw["state"]]
            result.append(mk_or(*(atom_condition(Atom("state", entity, "=", str(v))) for v in states)))
        if "state_not" in raw:
            states = raw["state_not"] if isinstance(raw["state_not"], list) else [raw["state_not"]]
            result.extend(atom_condition(Atom("state", entity, "!=", str(v))) for v in states)
        return mk_and(*result) if result else atom_condition(Atom("raw", raw=min_json(raw)))
    if ctype == "numeric_state":
        entity = str(raw.get("entity", ""))
        result: list[Cond] = []
        if raw.get("above") is not None:
            result.append(atom_condition(Atom("numeric", entity, ">", str(raw["above"]))))
        if raw.get("below") is not None:
            result.append(atom_condition(Atom("numeric", entity, "<", str(raw["below"]))))
        return mk_and(*result) if result else atom_condition(Atom("raw", raw=min_json(raw)))
    if ctype == "user":
        users = tuple(dict.fromkeys(str(x) for x in raw.get("users", [])))
        return atom_condition(Atom("user", users=users))
    return atom_condition(Atom("raw", raw=min_json(raw)))


def normalise_view_visible(raw: Any) -> Cond:
    """HA view visible is boolean or a list of user objects."""
    if raw is None or raw is True:
        return TRUE
    if raw is False:
        return FALSE
    if isinstance(raw, list) and all(isinstance(x, dict) and set(x) == {"user"} for x in raw):
        return atom_condition(Atom("user", users=tuple(str(x["user"]) for x in raw)))
    return atom_condition(Atom("raw", raw=min_json({"view_visible": raw})))


def min_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def condition_atoms(c: Cond) -> Iterator[Atom]:
    if c.op == "atom":
        yield c.args[0]
    elif c.op in {"and", "or"}:
        for child in c.args:
            yield from condition_atoms(child)


def replace_condition(c: Cond, fn) -> Cond:
    if c.op == "atom":
        return fn(c.args[0])
    if c.op == "and":
        return mk_and(*(replace_condition(x, fn) for x in c.args))
    if c.op == "or":
        return mk_or(*(replace_condition(x, fn) for x in c.args))
    return c


def is_menu_entity(entity: str) -> bool:
    return bool(MENU_ENTITY_RE.search(entity.split(".", 1)[-1]))


def menu_requirements(c: Cond) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for a in condition_atoms(c):
        if a.kind == "state" and a.op == "=" and is_menu_entity(a.entity):
            out.append((a.entity, a.value))
    return list(dict.fromkeys(out))


# ---------- YAML/card extraction ----------

@dataclass
class RawItem:
    seq: int
    view_path: str
    view_title: str
    section_title: str
    label: str
    kind: str
    local_condition: Cond
    primary_entities: tuple[str, ...] = ()
    content: str = ""
    url: str = ""
    navigation_target: str = ""
    selects_menu: tuple[str, str] | None = None
    service: str = ""
    action_target: tuple[str, ...] = ()
    confirmation: bool = False
    select_options: tuple[str, ...] = ()


@dataclass
class EntityCapability:
    entity: str
    modes: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    paths: set[str] = field(default_factory=set)
    conditions: list[Cond] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Convert a Home Assistant Lovelace YAML dashboard to one readable "
            "¦-delimited property database."
        )
    )
    p.add_argument("-i","--input", type=Path, help="Dashboard YAML")
    p.add_argument("-o", "--output", type=Path, help="Output file; stdout if omitted")
    p.add_argument(
        "--max-chars",
        type=int,
        default=0,
        help=(
            "Refuse to write if output exceeds this many Unicode characters; "
            "0 disables the limit"
        ),
    )
    p.add_argument(
        "--keep-image-paths",
        action="store_true",
        help="Keep image URLs; by default only image alt text is retained",
    )
    p.add_argument(
        "--stats",
        action="store_true",
        help="Print record counts, character size, byte size, and SHA-256 to stderr",
    )
    return p.parse_args()


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8", errors="strict").expandtabs(4))
    if not isinstance(data, dict) or not isinstance(data.get("views"), list):
        raise ValueError("input does not contain a top-level views list")
    return data


def human_entity(entity: str) -> str:
    object_id = entity.split(".", 1)[-1]
    words = object_id.replace("-", "_").split("_")
    while words and re.fullmatch(r"(?:switch|channel|button)\d*", words[-1], re.I):
        words.pop()
    deduped: list[str] = []
    for word in words:
        if deduped and re.sub(r"\d+$", "", word) == re.sub(r"\d+$", "", deduped[-1]):
            continue
        deduped.append(word)
    acronyms = {"ai": "AI", "co": "CO", "ev": "EV", "led": "LED", "pbx": "PBX", "sip": "SIP", "tv": "TV", "usb": "USB", "wifi": "WiFi", "ont": "ONT"}
    return " ".join(acronyms.get(w.lower(), w.capitalize()) for w in deduped if w)


def label_of(card: dict[str, Any], entity: str = "", fallback: str = "Dashboard item") -> str:
    for key in ("name", "title", "label", "textUnconfirmed", "textConfirmed"):
        value = card.get(key)
        if isinstance(value, str) and value.strip():
            return clean_inline(value)
    return human_entity(entity) if entity else fallback


def action_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"tap_action", "hold_action", "double_tap_action", "confirm_action"} and isinstance(child, dict):
                yield child
            elif key not in {"cards", "card", "sub_button", "sub_buttons", "buttons", "elements", "entities"}:
                yield from action_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from action_dicts(child)


def selected_option(card: dict[str, Any]) -> tuple[str, str] | None:
    for action in action_dicts(card):
        service = action.get("perform_action") or action.get("service")
        if service != "input_select.select_option":
            continue
        target = action.get("target") or {}
        data = action.get("data") or {}
        entity = target.get("entity_id")
        option = data.get("option")
        if isinstance(entity, str) and option is not None:
            return entity, str(option)
    return None


def navigation_target(card: dict[str, Any], dashboard_base: str) -> str:
    for action in action_dicts(card):
        path = action.get("navigation_path")
        if isinstance(path, str) and path:
            path = path.rstrip("/")
            return path.rsplit("/", 1)[-1]
    return ""


def action_service_and_targets(card: dict[str, Any]) -> tuple[str, tuple[str, ...], bool]:
    services: list[str] = []
    targets: list[str] = []
    confirmation = False
    for action in action_dicts(card):
        service = action.get("perform_action") or action.get("service")
        if isinstance(service, str):
            services.append(service)
        target = action.get("target") or {}
        entity_ids = target.get("entity_id") if isinstance(target, dict) else None
        if isinstance(entity_ids, str):
            targets.append(entity_ids)
        elif isinstance(entity_ids, list):
            targets.extend(str(x) for x in entity_ids)
        if action.get("confirmation") or action.get("action") == "call-service" and action.get("confirmation"):
            confirmation = True
    return "/".join(dict.fromkeys(services)), tuple(dict.fromkeys(targets)), confirmation


def classify(card_type: str, entities: Iterable[str], content: str, nav: str, select: tuple[str, str] | None) -> str:
    if nav or select:
        return "navigation"
    if content or card_type in {"markdown", "iframe"}:
        return "information"
    if card_type.startswith("custom:sip-"):
        return "action"
    domains = {e.split(".", 1)[0] for e in entities if "." in e}
    if domains & CONTROL_DOMAINS or card_type in {"button", "light", "thermostat", "custom:mushroom-fan-card", "custom:bubble-card", "custom:slide-confirm-card"}:
        return "control"
    if domains & STATUS_DOMAINS or card_type in {"gauge", "history-graph", "weather-forecast", "custom:clock-weather-card", "custom:advanced-camera-card"}:
        return "status"
    if card_type == "custom:assist-chat-card":
        return "assistant"
    return "item"


def extract_direct_entities(card: dict[str, Any]) -> tuple[str, ...]:
    found: list[str] = []
    entity_keys = {
        "entity", "camera_entity", "camera_image", "sun_entity",
        "temperature_sensor", "humidity_sensor", "status_entity",
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in entity_keys and isinstance(child, str) and ENTITY_RE.fullmatch(child):
                    found.append(child)
                elif key not in {"visibility", "conditions", "condition", "cards", "card", "sub_button", "sub_buttons", "buttons", "elements", "entities"}:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(card)
    _, action_targets, _ = action_service_and_targets(card)
    found.extend(action_targets)
    return tuple(dict.fromkeys(found))

def collect_menu_labels(data: dict[str, Any]) -> dict[tuple[str, str, str], str]:
    result: dict[tuple[str, str, str], str] = {}
    for index, view in enumerate(data.get("views", [])):
        if not isinstance(view, dict):
            continue
        vp = str(view.get("path", index))
        def visit(value: Any) -> None:
            if isinstance(value, dict):
                sel = selected_option(value)
                if sel:
                    result.setdefault((vp, sel[0], sel[1]), label_of(value, fallback=sel[1]))
                entity = value.get("entity")
                features = value.get("features")
                if isinstance(entity, str) and isinstance(features, list):
                    for feature in features:
                        if isinstance(feature, dict) and feature.get("type") == "select-options":
                            for option in feature.get("options", []):
                                result.setdefault((vp, entity, str(option)), str(option))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(view)
    return result


def extract_items(data: dict[str, Any]) -> tuple[list[RawItem], dict[str, dict[str, Any]], dict[tuple[str, str, str], str]]:
    menu_labels = collect_menu_labels(data)
    items: list[RawItem] = []
    views: dict[str, dict[str, Any]] = {}
    seq = 0

    def add_item(view_path: str, view_title: str, section_title: str, card: dict[str, Any], condition: Cond, entity_override: str = "", label_override: str = "", kind_override: str = "") -> None:
        nonlocal seq
        card_type = str(card.get("type", "unknown"))
        entities = (entity_override,) if entity_override else extract_direct_entities(card)
        content = card.get("content") if isinstance(card.get("content"), str) else ""
        url = card.get("url") if isinstance(card.get("url"), str) else ""
        nav = navigation_target(card, "")
        sel = selected_option(card)
        service, targets, confirmation = action_service_and_targets(card)
        select_options: list[str] = []
        if isinstance(card.get("entity"), str) and isinstance(card.get("features"), list):
            for feature in card["features"]:
                if isinstance(feature, dict) and feature.get("type") == "select-options":
                    select_options.extend(str(x) for x in feature.get("options", []))
        if targets:
            entities = tuple(dict.fromkeys((*entities, *targets)))
        label = label_override or label_of(card, entity_override or (entities[0] if entities else ""), card_type)
        kind = kind_override or classify(card_type, entities, content, nav, sel)
        # Skip pure layout/empty artefacts.
        if not (entities or content or url or nav or sel or service or kind in {"assistant", "action"}):
            return
        seq += 1
        items.append(RawItem(seq, view_path, view_title, section_title, label, kind, condition, entities, content, url, nav, sel, service, targets, confirmation, tuple(dict.fromkeys(select_options))))

    def walk_card(view_path: str, view_title: str, section_title: str, card: Any, inherited: Cond) -> None:
        if not isinstance(card, dict):
            return
        current = mk_and(inherited, normalise_condition(card.get("visibility")))
        if card.get("_dashboard_mcp_unsupported"):
            current = mk_and(current, atom_condition(Atom("raw", raw=min_json({"unsupported_card": card["_dashboard_mcp_unsupported"]}))))
        card_type = str(card.get("type", "unknown"))
        if card_type == "conditional":
            walk_card(view_path, view_title, section_title, card.get("card"), mk_and(current, normalise_condition(card.get("conditions"))))
            return
        if card_type in CONTAINER_TYPES:
            for child in card.get("cards", []):
                walk_card(view_path, view_title, section_title, child, current)
            return
        if card_type == "entities" and isinstance(card.get("entities"), list):
            group = label_of(card, fallback=section_title or "Entities")
            for entry in card["entities"]:
                if isinstance(entry, str):
                    child = {"type": "entity", "entity": entry, "name": human_entity(entry)}
                    walk_card(view_path, view_title, group, child, current)
                elif isinstance(entry, dict):
                    child = copy.deepcopy(entry)
                    child.setdefault("type", "entity")
                    walk_card(view_path, view_title, group, child, current)
            return
        if card_type == "history-graph" and isinstance(card.get("entities"), list):
            for entry in card["entities"]:
                if isinstance(entry, str):
                    add_item(view_path, view_title, section_title, {"type": "sensor", "entity": entry}, current, entity_override=entry, label_override=human_entity(entry), kind_override="status")
                elif isinstance(entry, dict) and isinstance(entry.get("entity"), str):
                    entity = entry["entity"]
                    add_item(view_path, view_title, section_title, entry, current, entity_override=entity, label_override=label_of(entry, entity), kind_override="status")
            return
        if card_type == "custom:slide-confirm-card" and isinstance(card.get("sliders"), list):
            for slider in card["sliders"]:
                if isinstance(slider, dict):
                    child = {"type": card_type, **slider}
                    add_item(view_path, view_title, section_title, child, current, label_override=label_of(slider, fallback="Confirmation"), kind_override="control")
            return

        # Main card record.
        add_item(view_path, view_title, section_title, card, current)

        # Nested sub-buttons are semantically separate actions/controls.
        for key in ("sub_button", "sub_buttons", "buttons"):
            children = card.get(key)
            child_lists: list[list[Any]] = []
            if isinstance(children, list):
                child_lists.append(children)
            elif isinstance(children, dict):
                child_lists.extend(v for v in children.values() if isinstance(v, list))
            for child_list in child_lists:
                for child in child_list:
                    if isinstance(child, dict):
                        child_cond = mk_and(current, normalise_condition(child.get("visibility")))
                        if child.get("_dashboard_mcp_unsupported"):
                            child_cond = mk_and(child_cond, atom_condition(Atom("raw", raw=min_json({"unsupported_card": child["_dashboard_mcp_unsupported"]}))))
                        add_item(view_path, view_title, section_title, child, child_cond)

    for index, view in enumerate(data.get("views", [])):
        if not isinstance(view, dict):
            continue
        vp = str(view.get("path", index))
        vt = str(view.get("title") or vp or "Dashboard")
        views[vp] = {
            "title": vt,
            "condition": mk_and(normalise_condition(view.get("visibility")), normalise_view_visible(view.get("visible"))),
            "initial": index == 0,
        }
        for section in view.get("sections", []):
            if not isinstance(section, dict):
                continue
            st = clean_inline(str(section.get("title") or ""))
            section_cond = mk_and(views[vp]["condition"], normalise_condition(section.get("visibility")))
            for card in section.get("cards", []):
                walk_card(vp, vt, st, card, section_cond)
        for card in view.get("cards", []):
            walk_card(vp, vt, "", card, views[vp]["condition"])
    return items, views, menu_labels


# ---------- Reachability ----------

def resolve_reachability(items: list[RawItem], views: dict[str, dict[str, Any]]) -> tuple[dict[str, Cond], dict[tuple[str, str, str], Cond], dict[int, Cond]]:
    view_entry: dict[str, Cond] = {vp: (meta["condition"] if meta["initial"] else FALSE) for vp, meta in views.items()}
    selector_entry: dict[tuple[str, str, str], Cond] = {}

    def resolve_local(item: RawItem, current_selectors: dict[tuple[str, str, str], Cond]) -> Cond:
        def repl(a: Atom) -> Cond:
            if a.kind == "state" and a.op == "=" and is_menu_entity(a.entity):
                # Search filters are user-selectable and are not authorization gates.
                if a.entity.split(".", 1)[-1].endswith("_search"):
                    return TRUE
                return current_selectors.get((item.view_path, a.entity, a.value), FALSE)
            return atom_condition(a)
        return replace_condition(item.local_condition, repl)

    # Fixed point: navigation and menu selectors can themselves live on reached views.
    for _ in range(100):
        new_view = dict(view_entry)
        new_selector = dict(selector_entry)
        for item in items:
            source = view_entry.get(item.view_path, FALSE)
            effective = mk_and(source, resolve_local(item, selector_entry))
            if item.navigation_target and item.navigation_target in views:
                key = item.navigation_target
                new_view[key] = mk_or(new_view.get(key, FALSE), effective, views[key]["condition"] if views[key]["initial"] else FALSE)
            if item.selects_menu:
                entity, option = item.selects_menu
                key = (item.view_path, entity, option)
                new_selector[key] = mk_or(new_selector.get(key, FALSE), effective)
            if item.select_options and item.primary_entities:
                menu_entity = item.primary_entities[0]
                for option in item.select_options:
                    key = (item.view_path, menu_entity, option)
                    new_selector[key] = mk_or(new_selector.get(key, FALSE), effective)
        if all(cond_key(new_view.get(k, FALSE)) == cond_key(view_entry.get(k, FALSE)) for k in set(new_view) | set(view_entry)) and all(cond_key(new_selector.get(k, FALSE)) == cond_key(selector_entry.get(k, FALSE)) for k in set(new_selector) | set(selector_entry)):
            view_entry, selector_entry = new_view, new_selector
            break
        view_entry, selector_entry = new_view, new_selector
    else:
        raise RuntimeError("reachability resolution did not converge")

    effective: dict[int, Cond] = {}
    for item in items:
        effective[item.seq] = mk_and(view_entry.get(item.view_path, FALSE), resolve_local(item, selector_entry))
    return view_entry, selector_entry, effective


# ---------- Compaction ----------

def clean_inline(value: str) -> str:
    return SPACE_RE.sub(" ", value.replace("\r", " ").replace("\n", " ")).strip()


def compact_text(value: str, keep_image_paths: bool) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = HTML_HREF_RE.sub(lambda m: f"{strip_tags(m.group(3))}<{m.group(2)}>", value)
    value = QR_TAG_RE.sub(lambda m: f"[QR:{m.group(2)}]", value)

    def replace_html_img(match: re.Match[str]) -> str:
        attrs = {m.group(1).lower(): m.group(3) for m in ATTR_RE.finditer(match.group(1))}
        src = attrs.get("src", "")
        alt = attrs.get("alt", "") or (Path(src).stem.replace("_", " ") if src else "image")
        return f"[image:{alt}<{src}>]" if keep_image_paths and src else f"[image:{alt}]"

    value = IMG_TAG_RE.sub(replace_html_img, value)
    if keep_image_paths:
        value = MARKDOWN_IMAGE_RE.sub(lambda m: f"[image:{m.group(1)}<{m.group(2)}>]", value)
    else:
        value = MARKDOWN_IMAGE_RE.sub(lambda m: f"[image:{m.group(1)}]" if m.group(1).strip() else "", value)
    value = MARKDOWN_LINK_RE.sub(lambda m: f"{m.group(1)}<{m.group(2)}>", value)
    # Remove Markdown line-continuation backslashes and emphasis markers.
    # Unknown angle-bracket placeholders such as <number> are deliberately kept.
    value = re.sub(r"\\\s*\n", "\n", value)
    value = value.replace("**", "").replace("__", "").replace("`", "")
    value = KNOWN_HTML_TAG_RE.sub(" ", value)
    value = html.unescape(value)
    value = BLANK_RE.sub("\n", value)
    lines = [SPACE_RE.sub(" ", line).strip() for line in value.split("\n")]
    value = NEWLINE_MARK.join(line for line in lines if line)
    return escape_field(value)


def strip_tags(value: str) -> str:
    return SPACE_RE.sub(" ", KNOWN_HTML_TAG_RE.sub(" ", value)).strip()


def escape_field(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\\", "\\\\").replace(DELIM, "\\" + DELIM)
    text = text.replace("\r", "").replace("\n", NEWLINE_MARK)
    return text


def encode_value(value: str) -> str:
    """Render a condition value compactly without hiding its meaning."""
    if SIMPLE_VALUE_RE.fullmatch(value):
        return value
    return "'" + value.replace("'", "''") + "'"


def render_condition(c: Cond, parent: str = "") -> str:
    """Render a complete condition using full, readable identifiers."""
    if c.op == "true":
        return "always"
    if c.op == "false":
        return "never"
    if c.op == "atom":
        a: Atom = c.args[0]
        if a.kind in {"state", "numeric"}:
            return f"{a.entity}{a.op}{encode_value(a.value)}"
        if a.kind == "user":
            users = ",".join(a.users)
            return f"home_assistant_user IN ({users})"
        return "unsupported_condition(" + escape_field(a.raw) + ")"

    joiner = " AND " if c.op == "and" else " OR "
    rendered = joiner.join(render_condition(x, c.op) for x in c.args)
    if parent and parent != c.op:
        return "(" + rendered + ")"
    return rendered


def location_for(
    item: RawItem,
    menu_labels: dict[tuple[str, str, str], str],
) -> str:
    parts = [item.view_title]
    for entity, option in menu_requirements(item.local_condition):
        if entity.split(".", 1)[-1].endswith("_search"):
            continue
        display = menu_labels.get((item.view_path, entity, option), option)
        if display and display not in parts:
            parts.append(display)
    if item.section_title and item.section_title not in parts:
        parts.append(item.section_title)
    return " > ".join(parts)


def instruction_labels(text: str) -> str:
    """Return readable labels for notices that must be repeated to the user."""
    labels: list[str] = []
    low = text.casefold()
    checks = (
        ("rule", "rule"),
        ("checking out", "checking_out"),
        ("check-out", "checking_out"),
        ("checkout", "checking_out"),
        ("caution", "caution"),
        ("warning", "warning"),
        ("important", "important"),
        ("note", "note"),
        ("privacy", "privacy"),
    )
    for needle, label in checks:
        if needle in low and label not in labels:
            labels.append(label)
    return ",".join(labels)


def make_database(
    data: dict[str, Any],
    keep_image_paths: bool,
) -> tuple[str, dict[str, int]]:
    items, views, menu_labels = extract_items(data)
    _, _, effective = resolve_reachability(items, views)

    capabilities: dict[str, EntityCapability] = {}
    info_rows: list[tuple[str, str, Cond, str, str]] = []
    action_rows: list[tuple[str, str, Cond, str, str]] = []

    for item in items:
        condition = effective[item.seq]
        section = location_for(item, menu_labels)

        if item.content or item.url:
            text = item.content or item.url
            info_rows.append(
                (item.label, section, condition, instruction_labels(text), text)
            )

        if item.kind in {"action", "assistant"} and not item.primary_entities:
            action_rows.append(
                (
                    item.label,
                    section,
                    condition,
                    item.service,
                    ",".join(item.action_target),
                )
            )

        for entity in item.primary_entities:
            if not entity or not ENTITY_RE.fullmatch(entity):
                continue
            # Menu/search helpers change dashboard navigation state and are not
            # executable device capabilities for the conversation agent.
            if is_menu_entity(entity):
                continue

            domain = entity.split(".", 1)[0]
            capability = (
                "control"
                if domain in CONTROL_DOMAINS or item.kind == "control"
                else "status"
            )
            cap = capabilities.setdefault(entity, EntityCapability(entity))
            cap.modes.add(capability)
            cap.names.add(item.label or human_entity(entity))
            cap.paths.add(section)
            cap.conditions.append(condition)

    header = DELIM.join(
        [
            "record_type",
            "section",
            "name",
            "entity_id",
            "capability",
            "condition",
            "labels",
            "service",
            "target",
            "text",
        ]
    )
    lines = [
        "Home Assistant property database.",
        "Fields are separated by ¦. The symbol ↵ represents a line break inside a field. A literal ¦ is written as \\¦.",
        "Conditions use full entity IDs. AND, OR, parentheses, =, !=, > and < have their ordinary meanings. always means unrestricted; never means unavailable. Treat an unknown or unsupported condition as denied.",
        "record_type is entity, information, or action. capability is control, status, information, or action. labels identify text that must also be stated, such as rule, checking_out, caution, warning, important, note, or privacy.",
        header,
    ]

    for entity in sorted(capabilities):
        cap = capabilities[entity]
        condition = mk_or(*cap.conditions)
        capability = "control" if "control" in cap.modes else "status"
        names = " / ".join(sorted(cap.names, key=lambda s: (len(s), s.casefold())))
        sections = " / ".join(sorted(cap.paths, key=str.casefold))
        lines.append(
            DELIM.join(
                [
                    "entity",
                    escape_field(sections),
                    escape_field(names),
                    escape_field(entity),
                    capability,
                    escape_field(render_condition(condition)),
                    "",
                    "",
                    "",
                    "",
                ]
            )
        )

    for name, section, condition, labels, text in sorted(
        info_rows,
        key=lambda row: (row[1].casefold(), row[0].casefold()),
    ):
        lines.append(
            DELIM.join(
                [
                    "information",
                    escape_field(section),
                    escape_field(name),
                    "",
                    "information",
                    escape_field(render_condition(condition)),
                    escape_field(labels),
                    "",
                    "",
                    compact_text(text, keep_image_paths),
                ]
            )
        )

    for name, section, condition, service, target in sorted(
        action_rows,
        key=lambda row: (row[1].casefold(), row[0].casefold()),
    ):
        lines.append(
            DELIM.join(
                [
                    "action",
                    escape_field(section),
                    escape_field(name),
                    "",
                    "action",
                    escape_field(render_condition(condition)),
                    "",
                    escape_field(service),
                    escape_field(target),
                    "",
                ]
            )
        )

    text = "\n".join(lines) + "\n"
    stats = {
        "items": len(items),
        "records": len(lines) - 5,
        "entities": len(capabilities),
        "information": len(info_rows),
        "actions": len(action_rows),
        "characters": len(text),
        "bytes": len(text.encode("utf-8")),
    }
    return text, stats


def main() -> None:
    args = parse_args()
    try:
        data = load_yaml(args.input)
        text, stats = make_database(data, args.keep_image_paths)
        if args.max_chars and len(text) > args.max_chars:
            raise ValueError(
                f"output is {len(text):,} characters, exceeding "
                f"--max-chars {args.max_chars:,}; nothing written"
            )

        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)

        if args.stats:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            print(" ".join(f"{key}={value}" for key, value in stats.items()), file=sys.stderr)
            print(f"sha256={digest}", file=sys.stderr)
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
