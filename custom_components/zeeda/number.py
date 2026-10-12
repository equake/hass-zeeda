"""Number: charging current (A) as a free-form value, for automations.

The charger only accepts discrete amperages, so the requested value is snapped
to the nearest accepted one. The select entity is the primary control; this
one is disabled by default and kept for automations that compute an amperage
(e.g. from solar surplus). Setting the current while a session is on is
handled by the coordinator (stop -> change -> start).
"""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZeedaConfigEntry
from .const import DEFAULT_CURRENT_OPTIONS
from .coordinator import ZeedaCoordinator
from .entity import ZeedaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZeedaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ZeedaCoordinator = entry.runtime_data
    async_add_entities(
        ZeedaCurrentNumber(coordinator, code) for code in coordinator.chargers
    )


class ZeedaCurrentNumber(ZeedaEntity, NumberEntity):
    """Max charging current in amperes."""

    _attr_translation_key = "max_current"
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:current-ac"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: ZeedaCoordinator, code: str) -> None:
        super().__init__(coordinator, code)
        self._attr_unique_id = f"{code}_max_current"
        options = [int(x) for x in self._charger.current_options if str(x).isdigit()]
        if not options:
            options = [int(x) for x in DEFAULT_CURRENT_OPTIONS]
        self._attr_native_min_value = float(min(options))
        self._attr_native_max_value = float(max(options))

    @property
    def native_value(self) -> float | None:
        value = self._charger.effective_max_current
        return float(value) if value else None

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.set_max_current(self._code, int(value))
