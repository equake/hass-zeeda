"""Select: charging mode (authorized / plug-and-charge / reservation)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZeedaConfigEntry
from .const import CHARGE_MODE_BY_SLUG, CHARGE_MODES, DOMAIN
from .coordinator import ZeedaCoordinator
from .entity import ZeedaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZeedaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ZeedaCoordinator = entry.runtime_data
    async_add_entities(
        ZeedaChargeModeSelect(coordinator, code) for code in coordinator.chargers
    )


class ZeedaChargeModeSelect(ZeedaEntity, SelectEntity):
    """Charging mode. Options are translated via their slugs."""

    _attr_translation_key = "charge_mode"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:cog-outline"

    def __init__(self, coordinator: ZeedaCoordinator, code: str) -> None:
        super().__init__(coordinator, code)
        self._attr_unique_id = f"{code}_charge_mode"
        self._attr_options = list(CHARGE_MODES.values())

    @property
    def current_option(self) -> str | None:
        return CHARGE_MODES.get(self._charger.effective_charge_mode)

    async def async_select_option(self, option: str) -> None:
        value = CHARGE_MODE_BY_SLUG.get(option)
        if value is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="invalid_charge_mode",
                translation_placeholders={"option": option},
            )
        await self._coordinator.set_charge_mode(self._code, value)
