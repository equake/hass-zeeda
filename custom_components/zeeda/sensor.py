"""Sensors: live charge status and meter values per charger."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZeedaConfigEntry
from .const import CONNECTOR_STATUS
from .coordinator import Charger, ZeedaCoordinator
from .entity import ZeedaEntity


@dataclass(frozen=True, kw_only=True)
class ZeedaSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Charger], object]


def _status_slug(charger: Charger) -> str | None:
    if charger.status is None:
        return None
    return CONNECTOR_STATUS.get(charger.status, "unknown")


_SENSORS: tuple[ZeedaSensorDescription, ...] = (
    ZeedaSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(CONNECTOR_STATUS.values()) | {"unknown"}),
        value_fn=_status_slug,
    ),
    ZeedaSensorDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value_fn=lambda c: c.power_kw,
    ),
    ZeedaSensorDescription(
        key="session_energy",
        translation_key="session_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda c: c.session_energy_kwh,
    ),
    ZeedaSensorDescription(
        key="total_energy",
        translation_key="total_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        # The meter reports a lifetime cumulative value; observed magnitude
        # (~1.0e6 for ~1 MWh) is in watt-hours. Native Wh, displayed as kWh.
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda c: c.total_energy,
    ),
    ZeedaSensorDescription(
        key="current",
        translation_key="current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
        value_fn=lambda c: c.current_a,
    ),
    ZeedaSensorDescription(
        key="voltage",
        translation_key="voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=0,
        value_fn=lambda c: c.voltage_v,
        entity_registry_enabled_default=False,
    ),
    ZeedaSensorDescription(
        key="duration",
        translation_key="duration",
        icon="mdi:timer-outline",
        value_fn=lambda c: c.duration,
    ),
    ZeedaSensorDescription(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.firmware,
    ),
    ZeedaSensorDescription(
        key="wifi",
        translation_key="wifi",
        icon="mdi:wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.wifi_name,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZeedaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ZeedaCoordinator = entry.runtime_data
    async_add_entities(
        ZeedaSensor(coordinator, code, desc)
        for code in coordinator.chargers
        for desc in _SENSORS
    )


class ZeedaSensor(ZeedaEntity, SensorEntity):
    entity_description: ZeedaSensorDescription

    def __init__(
        self, coordinator: ZeedaCoordinator, code: str, desc: ZeedaSensorDescription
    ) -> None:
        super().__init__(coordinator, code)
        self.entity_description = desc
        self._attr_unique_id = f"{code}_{desc.key}"

    @property
    def available(self) -> bool:
        # status/firmware should still read when the charger is offline.
        if self.entity_description.key in ("status", "firmware", "wifi"):
            return self._coordinator.connected
        return super().available

    @property
    def native_value(self):
        return self.entity_description.value_fn(self._charger)
