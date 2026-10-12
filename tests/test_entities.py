"""Tests for the charging-mode select and fault binary sensor."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from custom_components.zeeda.binary_sensor import _BINARY_SENSORS
from custom_components.zeeda.const import STATUS_CHARGING, STATUS_FAULT
from custom_components.zeeda.coordinator import Charger
from custom_components.zeeda.select import ZeedaChargeModeSelect
from custom_components.zeeda.sensor import _SENSORS, ZeedaSensor, _duration_seconds


@pytest.mark.asyncio
async def test_select_rejects_unknown_mode() -> None:
    select = ZeedaChargeModeSelect(Mock(), "Z123")

    with pytest.raises(HomeAssistantError):
        await select.async_select_option("bogus")


def test_fault_sensor_detects_fault_status() -> None:
    fault = next(desc for desc in _BINARY_SENSORS if desc.key == "fault")

    faulted = Charger(code="Z1", name="Garage", charge_point_id=1, status=STATUS_FAULT)
    assert fault.value_fn(faulted) is True

    charging = Charger(
        code="Z1", name="Garage", charge_point_id=1, status=STATUS_CHARGING
    )
    assert fault.value_fn(charging) is False

    with_error = Charger(
        code="Z1", name="Garage", charge_point_id=1, status=STATUS_CHARGING, error_code=1
    )
    assert fault.value_fn(with_error) is True


def test_plugged_sensor_follows_the_cable() -> None:
    plugged = next(desc for desc in _BINARY_SENSORS if desc.key == "plugged")

    def _value(status: int) -> bool:
        charger = Charger(code="Z1", name="Garage", charge_point_id=1, status=status)
        return plugged.value_fn(charger)

    assert [_value(status) for status in (1, 2, 3, 4, 5)] == [True] * 5
    assert [_value(status) for status in (0, 6, 7, 9, 10)] == [False] * 5


def test_capability_sensors_are_diagnostic_and_disabled_by_default() -> None:
    by_key = {desc.key: desc for desc in _BINARY_SENSORS}
    solar, load = by_key["solar"], by_key["load_balancing"]

    for desc in (solar, load):
        assert desc.entity_category is EntityCategory.DIAGNOSTIC
        assert desc.entity_registry_enabled_default is False

    with_solar = Charger(
        code="Z1", name="Garage", charge_point_id=1, support_solar=True
    )
    with_load = Charger(
        code="Z1", name="Garage", charge_point_id=1, support_load_balancing=True
    )
    assert solar.value_fn(with_solar) is True
    assert load.value_fn(with_load) is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("01:23:45", 5025),
        ("00:00:07", 7),
        ("12:34", 754),
        ("", None),
        (None, None),
        ("charging a while", None),
        ("1:00:00:00", None),
    ),
)
def test_duration_seconds_parses_the_meter_format(
    raw: object, expected: int | None
) -> None:
    assert _duration_seconds(raw) == expected


def test_duration_sensor_reports_seconds() -> None:
    charger = Charger(code="Z1", name="Garage", charge_point_id=1, duration="00:12:34")
    coordinator = Mock()
    coordinator.chargers = {"Z1": charger}
    description = next(desc for desc in _SENSORS if desc.key == "duration")

    sensor = ZeedaSensor(coordinator, "Z1", description)

    assert sensor.native_value == 754
