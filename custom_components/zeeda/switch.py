"""Switch: start / stop charging."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZeedaConfigEntry
from .const import STATUS_CHARGING, STATUS_WAITING
from .coordinator import ZeedaCoordinator
from .entity import ZeedaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZeedaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ZeedaCoordinator = entry.runtime_data
    async_add_entities(ZeedaChargeSwitch(coordinator, code) for code in coordinator.chargers)


class ZeedaChargeSwitch(ZeedaEntity, SwitchEntity):
    """On = charging/active; toggling sends StartCharge / StopCharge."""

    _attr_translation_key = "charge"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: ZeedaCoordinator, code: str) -> None:
        super().__init__(coordinator, code)
        self._attr_unique_id = f"{code}_charge"

    @property
    def available(self) -> bool:
        return self._coordinator.connected and self._charger.online

    @property
    def is_on(self) -> bool:
        # "On" while a session is active: charging or waiting/suspended.
        return self._charger.status in ({STATUS_CHARGING} | STATUS_WAITING)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._coordinator.start_charge(self._code)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.stop_charge(self._code)
