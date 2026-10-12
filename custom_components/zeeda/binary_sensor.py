"""Binary sensors: connectivity, charging, fault."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZeedaConfigEntry
from .const import PLUGGED_STATUSES, STATUS_CHARGING, STATUS_FAULT
from .coordinator import Charger, ZeedaCoordinator
from .entity import ZeedaEntity


@dataclass(frozen=True, kw_only=True)
class ZeedaBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[Charger], bool | None]
    always_available: bool = False


_BINARY_SENSORS: tuple[ZeedaBinaryDescription, ...] = (
    ZeedaBinaryDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.online,
        always_available=True,
    ),
    ZeedaBinaryDescription(
        key="plugged",
        translation_key="plugged",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda c: c.status in PLUGGED_STATUSES,
    ),
    ZeedaBinaryDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda c: c.status == STATUS_CHARGING,
    ),
    ZeedaBinaryDescription(
        key="fault",
        translation_key="fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: bool(c.error_code) or c.status == STATUS_FAULT,
    ),
    # Static capabilities reported by getChargePointDetail; only useful to the
    # few chargers that have them, so they stay disabled until enabled by hand.
    ZeedaBinaryDescription(
        key="solar",
        translation_key="solar",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:solar-power",
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.support_solar,
    ),
    ZeedaBinaryDescription(
        key="load_balancing",
        translation_key="load_balancing",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:home-lightning-bolt-outline",
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.support_load_balancing,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZeedaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ZeedaCoordinator = entry.runtime_data
    async_add_entities(
        ZeedaBinarySensor(coordinator, code, desc)
        for code in coordinator.chargers
        for desc in _BINARY_SENSORS
    )


class ZeedaBinarySensor(ZeedaEntity, BinarySensorEntity):
    entity_description: ZeedaBinaryDescription

    def __init__(
        self, coordinator: ZeedaCoordinator, code: str, desc: ZeedaBinaryDescription
    ) -> None:
        super().__init__(coordinator, code)
        self.entity_description = desc
        self._attr_unique_id = f"{code}_{desc.key}"

    @property
    def available(self) -> bool:
        if self.entity_description.always_available:
            return self._charger.reachable
        return super().available

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self._charger)
