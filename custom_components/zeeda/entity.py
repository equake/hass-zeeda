"""Base entity shared by all Zeeda platforms."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity

from .coordinator import ZeedaCoordinator


class ZeedaEntity(Entity):
    """Common base: device_info, availability and coordinator listener wiring.

    One entity belongs to one charger (keyed by its chargePointCode).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: ZeedaCoordinator, code: str) -> None:
        super().__init__()
        self._coordinator = coordinator
        self._code = code
        self._remove_listener: Callable[[], None] | None = None

    @property
    def _charger(self):
        return self._coordinator.chargers[self._code]

    @property
    def device_info(self):
        return self._coordinator.device_info(self._code)

    @property
    def available(self) -> bool:
        return self._coordinator.connected and self._charger.online

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_listener = self._coordinator.async_add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
