"""Number: charging current (A), adjustable live.

Setting the current while a session is actively charging is handled by the
coordinator (stop -> change -> start), so this can be moved at any time for
dynamic power control with the car plugged in.
"""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZeedaConfigEntry
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

    def __init__(self, coordinator: ZeedaCoordinator, code: str) -> None:
        super().__init__(coordinator, code)
        self._attr_unique_id = f"{code}_max_current"
        options = [int(x) for x in self._charger.current_options if str(x).isdigit()]
        self._attr_native_min_value = float(min(options)) if options else 6.0
        self._attr_native_max_value = float(max(options)) if options else 32.0

    @property
    def available(self) -> bool:
        return self._charger.reachable and self._charger.online

    @property
    def native_value(self) -> float | None:
        return float(self._charger.max_current) if self._charger.max_current else None

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.set_max_current(self._code, int(value))
