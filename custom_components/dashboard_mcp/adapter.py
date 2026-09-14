"""Only this module depends on Lovelace's internal dashboard loader."""

from collections.abc import Mapping
import json

from homeassistant.exceptions import HomeAssistantError


def dashboard_objects(hass) -> dict:
    data = hass.data.get("lovelace")
    objects = getattr(data, "dashboards", None)
    if objects is None and isinstance(data, Mapping):
        objects = data.get("dashboards")
    if not isinstance(objects, Mapping):
        raise HomeAssistantError("Dashboard MCP: Lovelace dashboard registry is unavailable.")
    return objects


def dashboard_choices(hass) -> list[dict]:
    result = []
    for path, obj in dashboard_objects(hass).items():
        metadata = getattr(obj, "config", None) or {}
        # Store an identity rather than the mutable title or URL where possible.
        identity = f"storage:{metadata['id']}" if metadata.get("id") else f"path:{path}" if path is not None else "default"
        result.append({"id": identity, "url_path": path, "title": metadata.get("title") or path or "Overview", "mode": getattr(obj, "mode", "unknown")})
    return result


async def load_dashboards(hass, selected: list[str]) -> tuple[list[dict], dict]:
    objects = dashboard_objects(hass)
    choices = {d["id"]: d for d in dashboard_choices(hass)}
    loaded = {}
    metadata = []
    for identity in selected:
        if identity not in choices:
            raise HomeAssistantError("A selected dashboard no longer exists. Update Dashboard MCP settings.")
        info = choices[identity]
        try:
            # Force YAML reload so !include changes are noticed too. Storage mode
            # returns its current in-memory configuration. Never edit .storage.
            config = await objects[info["url_path"]].async_load(True)
        except Exception as exc:
            raise HomeAssistantError("A selected dashboard could not be loaded. Generated dashboards require an explicit configuration.") from exc
        # HA's YAML loader can return annotated dict/string subclasses. Convert
        # through its JSON-compatible data model before the optional PyYAML dump.
        loaded[identity] = json.loads(json.dumps(config, ensure_ascii=False))
        metadata.append(info)
    return metadata, loaded
