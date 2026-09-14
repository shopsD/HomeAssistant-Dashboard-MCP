"""Dashboard MCP custom integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.helpers import llm

from .api import DashboardAPI
from .manager import DashboardManager


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    manager = DashboardManager(hass, entry)
    entry.runtime_data = manager
    entry.async_on_unload(llm.async_register_api(hass, DashboardAPI(hass, manager)))
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
    await manager.index.start()
    # Options are read on every call. Revocations do not wait for a reload or poll.
    return True

async def _options_updated(hass, entry):
    entry.runtime_data.index.invalidate()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR]):
        return False
    entry.runtime_data.active = False
    await entry.runtime_data.index.stop()
    return True
