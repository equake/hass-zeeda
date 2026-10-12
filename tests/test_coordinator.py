"""Tests for parsing real-time WebSocket payloads."""

from __future__ import annotations

from custom_components.zeeda.coordinator import Charger, ZeedaCoordinator


def test_apply_meter_values_payload() -> None:
    coordinator = object.__new__(ZeedaCoordinator)
    charger = Charger(code="Z123", name="Garage", charge_point_id=1)

    applied = coordinator._apply_payload(
        charger,
        "MeterValues",
        {
            "connectorStatus": 2,
            "chargingPower": "7.2",
            "chargingEnergyValue": 3.456,
            "energyValue": 12345.0,
            "timeConsume": "00:30:00",
            "currentList": [{"currentValue": 32.0}],
            "voltageList": [{"voltageValue": 230.0}],
        },
    )

    assert applied is True
    assert charger.status == 2
    assert charger.power_kw == 7.2
    assert charger.session_energy_kwh == 3.456
    assert charger.total_energy == 12345.0
    assert charger.duration == "00:30:00"
    assert charger.current_a == 32.0
    assert charger.voltage_v == 230.0
    assert charger.raw["push"]["action"] == "MeterValues"


def test_apply_status_notification_without_meter_values() -> None:
    coordinator = object.__new__(ZeedaCoordinator)
    charger = Charger(code="Z123", name="Garage", charge_point_id=1)

    assert coordinator._apply_payload(
        charger, "StatusNotification", {"connectorStatus": 1}
    )
    assert charger.status == 1
    assert charger.power_kw is None


def test_ignore_payload_without_supported_fields() -> None:
    coordinator = object.__new__(ZeedaCoordinator)
    charger = Charger(code="Z123", name="Garage", charge_point_id=1)

    assert not coordinator._apply_payload(charger, "Unknown", {"message": "ignored"})
    assert charger.raw == {}
