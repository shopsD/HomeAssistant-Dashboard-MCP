"""Diagnostic sensor for the property catalogue embedding index."""
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([EmbeddingStatusSensor(entry)])


class EmbeddingStatusSensor(SensorEntity):
    _attr_name = "Dashboard MCP embedding index"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["disabled", "indexing", "updating", "ready", "error"]
    _attr_icon = "mdi:database-search"

    def __init__(self, entry):
        self._index = entry.runtime_data.index
        self._attr_unique_id = f"{entry.entry_id}_embedding_index"

    @property
    def native_value(self):
        return self._index.status["state"]

    @property
    def extra_state_attributes(self):
        return {k: v for k, v in self._index.status.items() if k != "state"}

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(
            self.hass, self._index.signal, self.async_write_ha_state))
