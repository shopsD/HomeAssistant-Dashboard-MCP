"""Pure three-valued condition evaluation; never execute dashboard templates."""

from dataclasses import dataclass
import json
import math
from typing import Any, Mapping

from .converter import Cond


@dataclass(frozen=True)
class Decision:
    value: bool | None
    reason: str

    @property
    def status(self) -> str:
        return {True: "allowed", False: "restricted", None: "undetermined"}[self.value]


def combine(op: str, children: list[Decision]) -> Decision:
    values = [x.value for x in children]
    if op == "and":
        if False in values:
            return Decision(False, "At least one required condition is false.")
        if None in values:
            return Decision(None, "At least one required condition cannot be evaluated.")
        return Decision(True, "All required conditions are true.")
    if True in values:
        return Decision(True, "At least one alternative condition is true.")
    if None in values:
        return Decision(None, "An alternative condition cannot be evaluated.")
    return Decision(False, "No alternative condition is true.")


def _state_value(entity: str, states: Mapping, attribute: str | None = None) -> Any:
    item = states.get(entity)
    if not item or item.get("state") in {None, "unknown", "unavailable"}:
        return None
    return item.get("attributes", {}).get(attribute) if attribute else item["state"]


def evaluate_raw(raw: Any, states: Mapping, user_id: str | None) -> Decision:
    unknown = Decision(None, "Unsupported or incomplete condition.")
    if isinstance(raw, list):
        return combine("and", [evaluate_raw(x, states, user_id) for x in raw])
    if not isinstance(raw, dict):
        return unknown
    kind = raw.get("condition")
    if kind in {"and", "or", "not"}:
        if set(raw) - {"condition", "conditions"} or not isinstance(raw.get("conditions"), list):
            return unknown
        decision = combine("or" if kind == "or" else "and", [evaluate_raw(x, states, user_id) for x in raw["conditions"]])
        if kind == "not":
            return Decision(None if decision.value is None else not decision.value, "NOT of the nested conditions.")
        return decision
    if kind == "user":
        if set(raw) - {"condition", "users"} or not isinstance(raw.get("users"), list):
            return unknown
        return Decision(user_id in raw["users"] if user_id else None, "Configured dashboard viewer user check.")
    if kind not in {"state", "numeric_state"}:
        return unknown
    allowed_keys = {"condition", "entity", "attribute", "state", "state_not"} if kind == "state" else {"condition", "entity", "attribute", "above", "below"}
    if set(raw) - allowed_keys or not isinstance(raw.get("entity"), str):
        return unknown
    value = _state_value(raw["entity"], states, raw.get("attribute"))
    if value is None:
        return Decision(None, "Condition entity or attribute is unavailable.")
    checks = []
    if kind == "state":
        for key in ("state", "state_not"):
            if key in raw:
                expected = raw[key] if isinstance(raw[key], list) else [raw[key]]
                match = str(value) in [str(x) for x in expected]
                checks.append(Decision(match if key == "state" else not match, "State comparison."))
    else:
        try:
            value = float(value)
            if not math.isfinite(value):
                return unknown
            for key in ("above", "below"):
                if key in raw:
                    threshold = float(raw[key])
                    if not math.isfinite(threshold):
                        return unknown
                    checks.append(Decision(value > threshold if key == "above" else value < threshold, "Numeric comparison."))
        except (TypeError, ValueError, OverflowError):
            return unknown
    return combine("and", checks) if checks else unknown


def evaluate(condition: Cond, states: Mapping, user_id: str | None = None) -> Decision:
    if condition.op in {"true", "false"}:
        return Decision(condition.op == "true", "Unconditional." if condition.op == "true" else "No reachable dashboard route.")
    if condition.op in {"and", "or"}:
        return combine(condition.op, [evaluate(x, states, user_id) for x in condition.args])
    if condition.op != "atom":
        return Decision(None, "Unsupported condition operator.")
    atom = condition.args[0]
    if atom.kind == "raw":
        try:
            return evaluate_raw(json.loads(atom.raw), states, user_id)
        except (ValueError, TypeError):
            return Decision(None, "Unsupported condition.")
    if atom.kind == "user":
        return Decision(user_id in atom.users if user_id else None, "Configured dashboard viewer user check.")
    if atom.kind == "state":
        return evaluate_raw({"condition": "state", "entity": atom.entity, "state" if atom.op == "=" else "state_not": atom.value}, states, user_id)
    if atom.kind == "numeric" and atom.op in {">", "<"}:
        return evaluate_raw({"condition": "numeric_state", "entity": atom.entity, "above" if atom.op == ">" else "below": atom.value}, states, user_id)
    return Decision(None, "Unsupported condition.")
