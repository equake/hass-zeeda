"""Tests for parsing real-time WebSocket payloads."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.zeeda.api import ZeedaAuthError
from custom_components.zeeda.const import WS_ACTION_OFFLINE, WS_ACTION_ONLINE
from custom_components.zeeda.coordinator import (
    Charger,
    ZeedaCoordinator,
    _snap_current,
    _to_float,
)


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


@pytest.mark.parametrize(
    ("action", "initial_online", "expected_online"),
    (
        (WS_ACTION_ONLINE, False, True),
        (WS_ACTION_OFFLINE, True, False),
    ),
)
def test_online_offline_push_updates_connectivity_immediately(
    action: str, initial_online: bool, expected_online: bool
) -> None:
    coordinator = object.__new__(ZeedaCoordinator)
    charger = Charger(
        code="Z123",
        name="Garage",
        charge_point_id=1,
        online=initial_online,
    )
    coordinator.chargers = {charger.code: charger}
    coordinator.connected = False
    coordinator._notify = Mock()
    coordinator._ws_debounce = None
    coordinator.hass = Mock()

    coordinator._on_ws_push(charger.code, {"action": action})

    assert charger.reachable is True
    assert charger.online is expected_online
    assert coordinator.connected is True
    coordinator._notify.assert_called_once_with()
    coordinator.hass.loop.call_later.assert_not_called()


def test_snap_current_rounds_to_nearest_selectable_value() -> None:
    options = ("8", "16", "32")

    assert _snap_current(options, 13) == 16
    assert _snap_current(options, 40) == 32
    assert _snap_current(options, 2) == 8


def test_snap_current_tie_snaps_down() -> None:
    """12 A is equidistant from 8 and 16; the lower (safer) value wins."""
    assert _snap_current(("8", "16", "32"), 12) == 8


def test_snap_current_passthrough_without_selectable_values() -> None:
    assert _snap_current((), 20) == 20
    assert _snap_current(("auto",), 20) == 20


def test_apply_payload_coerces_numeric_strings_to_floats() -> None:
    """The cloud is not type-stable; sensors need actual floats."""
    coordinator = object.__new__(ZeedaCoordinator)
    charger = Charger(code="Z123", name="Garage", charge_point_id=1)

    assert coordinator._apply_payload(
        charger,
        "MeterValues",
        {
            "chargingPower": "7.2",
            "chargingEnergyValue": "3.456",
            "energyValue": "12345",
            "currentList": [{"currentValue": "32"}],
            "voltageList": [{"voltageValue": "230"}],
        },
    )

    assert charger.power_kw == 7.2
    assert charger.session_energy_kwh == 3.456
    assert charger.total_energy == 12345.0
    assert charger.current_a == 32.0
    assert charger.voltage_v == 230.0


@pytest.mark.asyncio
async def test_refresh_one_coerces_numeric_strings_from_rest() -> None:
    coordinator = object.__new__(ZeedaCoordinator)
    coordinator._api = Mock()
    coordinator._api.get_status_info = AsyncMock(
        return_value={
            "connectorStatus": 2,
            "currentMeterValue": {
                "chargingPower": "7.2",
                "chargingEnergyValue": "3.456",
                "energyValue": "1234567",
                "timeConsume": "01:00:00",
                "currentList": [{"currentValue": "32"}],
                "voltageList": [{"voltageValue": "230"}],
            },
        }
    )
    charger = Charger(code="Z123", name="Garage", charge_point_id=1)

    await coordinator._refresh_one("Z123", charger, full=False)

    assert charger.reachable is True
    assert charger.status == 2
    assert charger.power_kw == 7.2
    assert charger.session_energy_kwh == 3.456
    assert charger.total_energy == 1234567.0
    assert charger.duration == "01:00:00"
    assert charger.current_a == 32.0
    assert charger.voltage_v == 230.0


def test_to_float_rejects_unusable_values() -> None:
    assert _to_float("abc") is None
    assert _to_float("") is None
    assert _to_float(None) is None
    assert _to_float("7.2") == 7.2


def _runtime_auth_coordinator() -> tuple[ZeedaCoordinator, Charger]:
    """A coordinator whose API rejects both reads and the re-login."""
    coordinator = object.__new__(ZeedaCoordinator)
    coordinator.entry = Mock()
    coordinator.hass = None
    coordinator._account = "user@example.com"
    coordinator._password = "secret"
    coordinator._timezone = "America/Sao_Paulo"
    coordinator._api = Mock()
    coordinator._api.get_status_info = AsyncMock(side_effect=ZeedaAuthError("expired"))
    coordinator._api.login = AsyncMock(side_effect=ZeedaAuthError("bad credentials"))
    coordinator._session = None
    coordinator._ws = {}
    coordinator._listeners = set()
    coordinator.connected = True
    coordinator._auth_failures = 0
    coordinator._auth_cooldown_until = 0.0
    coordinator._reauth_warned = False
    coordinator._available = {}
    coordinator._offline_since = {}
    coordinator._offline_issues = set()
    charger = Charger(code="Z123", name="Garage", charge_point_id=1)
    coordinator.chargers = {"Z123": charger}
    return coordinator, charger


@pytest.mark.asyncio
async def test_refresh_starts_reauth_when_runtime_login_rejected() -> None:
    coordinator, charger = _runtime_auth_coordinator()

    # Must not raise: the debounced WS refresh calls this fire-and-forget.
    await coordinator.async_refresh()

    assert coordinator.connected is False
    assert charger.reachable is False
    coordinator.entry.async_start_reauth.assert_called_once_with(None)
    assert coordinator._auth_failures == 1


@pytest.mark.asyncio
async def test_refresh_stays_quiet_while_login_backs_off() -> None:
    coordinator, _ = _runtime_auth_coordinator()

    await coordinator.async_refresh()
    await coordinator.async_refresh()

    # Still exactly one reauth trigger: the second attempt hit the backoff.
    coordinator.entry.async_start_reauth.assert_called_once_with(None)
    assert coordinator._auth_failures == 1
