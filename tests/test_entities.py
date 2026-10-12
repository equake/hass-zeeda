"""Tests for the charging-mode select and fault binary sensor."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.zeeda.binary_sensor import _BINARY_SENSORS
from custom_components.zeeda.const import STATUS_CHARGING, STATUS_FAULT
from custom_components.zeeda.coordinator import Charger
from custom_components.zeeda.select import ZeedaChargeModeSelect


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
