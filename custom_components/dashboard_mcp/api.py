"""Custom Home Assistant LLM API served by the native MCP server."""

import voluptuous as vol

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from .const import API_ID, PROMPT

ENTITY = vol.All(str, vol.Match(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$"))
MODE = {
    vol.Optional("mode", description=(
        "Override condition evaluation for this call: 'dumb' evaluates visibility "
        "conditions server-side and withholds restricted content; 'smart' returns "
        "conditions for the caller to evaluate before use. Omit to use the configured "
        "default. Available only when Allow tool override is enabled."
    )): vol.In(["smart", "dumb"]),
}
PAGE = {
    **MODE,
    vol.Optional("dashboard_id", description=(
        "Only return records belonging to this selected dashboard ID. "
        "Use list_dashboards to discover IDs; omit to include all selected dashboards."
    )): vol.All(str, vol.Length(min=1, max=200)),
    vol.Optional("limit", default=500, description=(
        "Maximum records in this page, 1 to 5000. Default 500."
    )): vol.All(int, vol.Range(min=1, max=5000)),
    vol.Optional("offset", default=0, description=(
        "Zero-based position of the first record in this page. "
        "Use next_offset from the previous response to continue."
    )): vol.All(int, vol.Range(min=0)),
    vol.Optional("revision", description=(
        "Revision returned by the first page. Supply it on subsequent pages "
        "to detect changed catalogue content or permission decisions."
    )): str,
    vol.Optional("include_states", default=False, description=(
        "Include available live entity states and, when configured, states "
        "of entities referenced in record conditions. Default false."
    )): bool,
    vol.Optional("format", default="records", description=(
        "'records' returns structured JSON objects (default). 'delimited' returns "
        "a compact text table with a header row; fields are separated by ¦, "
        "line breaks within fields are encoded as ↵, and literal field delimiters "
        "are escaped as \\¦."
    )): vol.In(["records", "delimited"]),
    vol.Optional("max_chars", default=250000, description=(
        "Maximum response size in characters. Default 250000. "
        "Use 0 for no character limit. Exceeding this raises an "
        "error; it does not truncate the response."
    )): vol.All(int, vol.Range(min=0)),
}
TOOLS = {
    "list_dashboards": ("List only the dashboards selected for this property database.", vol.Schema(MODE)),
    "search_dashboard_context": (
        "FIRST lookup for every property question or action. Search entity names, IDs, panels, rules and information. Returns complete conditions and evaluated permissions in dumb mode. Restricted/error is not a web fallback. Set include_states for current values.",
        vol.Schema({vol.Required("query", description="Required search text, 1 to 300 characters. Matches dashboard names, sections, entities, rules and information."): vol.All(str, vol.Length(min=1, max=300)), **PAGE}),
    ),
    "get_dashboard_context": ("Read a page of the property catalogue. Use revision from the previous page to prevent mixing changed permissions. Normally search first.", vol.Schema(PAGE)),
    "get_entity_state": ("Read one permitted entity's current state. Does not execute actions or grant control. Blocklist always applies.", vol.Schema({vol.Required("entity_id", description="Full Home Assistant entity ID to read, such as sensor.kitchen_temperature."): ENTITY, **MODE})),
    "get_entity_states": ("Read up to 5000 permitted entities in a single current snapshot. Default is 50. Does not execute actions.", vol.Schema({vol.Required("entity_ids", description="List of 1 to 5000 complete Home Assistant entity IDs to read in one request."): vol.All([ENTITY], vol.Length(min=1, max=5000)), **MODE})),
    "get_dashboard_yaml": ("Inspect resolved dashboard YAML with entity redaction. Optional smart-mode tool; not evaluated permissions.", vol.Schema({vol.Required("dashboard_id", description="Selected dashboard ID whose resolved redacted YAML should be inspected."): str, **MODE})),
}


class DashboardTool(llm.Tool):
    def __init__(self, manager, name):
        self.manager = manager
        self.name = name
        self.description, schema = TOOLS[name]
        if manager.options["allow_tool_override"]:
            self.parameters = schema
        else:
            # Hide mode completely from the agent-visible tool schema.
            self.parameters = vol.Schema({key: value for key, value in schema.schema.items() if (key.schema if isinstance(key, vol.Marker) else key) != "mode"})

    async def async_call(self, hass, tool_input, llm_context):
        # HA 2026.9's APIInstance dispatch does not itself apply tool.parameters.
        try:
            if "mode" in tool_input.tool_args and not self.manager.options["allow_tool_override"]:
                raise HomeAssistantError("Per-tool mode override is disabled.")
            args = self.parameters(tool_input.tool_args)
        except vol.Invalid as exc:
            raise HomeAssistantError("Invalid dashboard tool arguments.") from exc
        return await self.manager.call(self.name, args)


class DashboardAPI(llm.API):
    def __init__(self, hass, manager):
        super().__init__(hass=hass, id=API_ID, name="Dashboard MCP")
        self.manager = manager

    async def async_get_api_instance(self, llm_context):
        if not self.manager.active:
            raise HomeAssistantError("Dashboard MCP is unloaded.")
        # Independent of any Assist-exposed entities or service execution tools.
        names = [n for n in TOOLS if n != "get_dashboard_yaml"]
        if self.manager.options["enable_yaml_export"] and (
            self.manager.options["mode"] == "smart" or self.manager.options["allow_tool_override"]
        ):
            names.append("get_dashboard_yaml")
        return llm.APIInstance(api=self, api_prompt=PROMPT, llm_context=llm_context,
            tools=[DashboardTool(self.manager, name) for name in names])
