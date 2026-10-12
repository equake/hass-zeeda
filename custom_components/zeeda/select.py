"""Select: charging mode, and charging current as a dropdown.

The charger only accepts the discrete amperages advertised by ``currentList``
(e.g. 8/16/32 A), so the current is a select of exactly those values; the
free-form number entity stays available (disabled by default) for automations
that compute an amperage.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZeedaConfigEntry
from .const import CHARGE_MODE_BY_SLUG, CHARGE_MODES, DEFAULT_CURRENT_OPTIONS, DOMAIN
from .coordinator import ZeedaCoordinator
from .entity import ZeedaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZeedaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ZeedaCoordinator = entry.runtime_data
    async_add_entities(
        entity
        for code in coordinator.chargers
        for entity in (
            ZeedaChargeModeSelect(coordinator, code),
            ZeedaCurrentSelect(coordinator, code),
        )
    )


def _current_label(amps: int) -> str:
    return f"{amps} A"


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


class ZeedaCurrentSelect(ZeedaEntity, SelectEntity):
    """Charging current, limited to the amperages the charger accepts."""

    _attr_translation_key = "max_current"
    _attr_icon = "mdi:current-ac"

    def __init__(self, coordinator: ZeedaCoordinator, code: str) -> None:
        super().__init__(coordinator, code)
        self._attr_unique_id = f"{code}_max_current_select"
        values = sorted(
            {int(x) for x in self._charger.current_options if str(x).isdigit()}
        ) or [int(x) for x in DEFAULT_CURRENT_OPTIONS]
        self._amps_by_option = {_current_label(v): v for v in values}
        self._attr_options = list(self._amps_by_option)

    @property
    def current_option(self) -> str | None:
        value = self._charger.effective_max_current
        if value is None:
            return None
        option = _current_label(int(value))
        return option if option in self._amps_by_option else None

    async def async_select_option(self, option: str) -> None:
        amps = self._amps_by_option.get(option)
        if amps is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_current",
                translation_placeholders={
                    "option": option,
                    "options": ", ".join(self._attr_options),
                },
            )
        await self._coordinator.set_max_current(self._code, amps)
