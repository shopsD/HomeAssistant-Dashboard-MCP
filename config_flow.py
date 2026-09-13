"""UI configuration: explicit dashboard and entity selections."""

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .adapter import dashboard_choices
from .const import DEFAULT_OPTIONS, DOMAIN


def schema(hass, current):
    choices = [{"value": d["id"], "label": f"{d['title']} ({d['mode']})"} for d in dashboard_choices(hass)]
    # Keep removed selections visible so the user can revoke them explicitly.
    known = {x["value"] for x in choices}
    choices += [{"value": d, "label": f"Missing dashboard: {d}"} for d in current["dashboards"] if d not in known]
    return vol.Schema({
        vol.Optional("dashboards", default=current["dashboards"]): selector.SelectSelector(selector.SelectSelectorConfig(options=choices, multiple=True, mode="dropdown")),
        vol.Optional("blocked_entities", default=current["blocked_entities"]): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True)),
        vol.Required("mode", default=current["mode"]): selector.SelectSelector(selector.SelectSelectorConfig(options=[{"value": "dumb", "label": "Dumb — server evaluates conditions"}, {"value": "smart", "label": "Smart — agent evaluates conditions"}])),
        vol.Required("include_condition_states", default=current["include_condition_states"]): bool,
        vol.Optional("extra_read_entities", default=current["extra_read_entities"]): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True)),
        vol.Optional("viewer_user_id", default=current["viewer_user_id"]): selector.TextSelector(),
        vol.Required("keep_image_paths", default=current["keep_image_paths"]): bool,
        vol.Required("enable_yaml_export", default=current["enable_yaml_export"]): bool,
    })


class DashboardConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Dashboard MCP", data={**DEFAULT_OPTIONS, **user_input})
        return self.async_show_form(step_id="user", data_schema=schema(self.hass, DEFAULT_OPTIONS))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return DashboardOptionsFlow()


class DashboardOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data={**DEFAULT_OPTIONS, **user_input})
        current = {**DEFAULT_OPTIONS, **self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=schema(self.hass, current))
