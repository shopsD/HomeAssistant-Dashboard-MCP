"""Dashboard MCP custom integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from .api import DashboardAPI
from .manager import DashboardManager


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    manager = DashboardManager(hass, entry)
    entry.runtime_data = manager
    entry.async_on_unload(llm.async_register_api(hass, DashboardAPI(hass, manager)))
    # Options are read on every call. Revocations do not wait for a reload or poll.
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry.runtime_data.active = False
    return True
