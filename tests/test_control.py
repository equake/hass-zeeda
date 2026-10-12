"""Tests for control commands: pending state, confirmation and reporting."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.zeeda import coordinator as coordinator_module
from custom_components.zeeda.api import ZeedaCommandError
from custom_components.zeeda.coordinator import Charger, ZeedaCoordinator

ACK = {"success": True}
_real_sleep = asyncio.sleep


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> list:
    """No real sleeping; capture async_call_later callbacks instead of scheduling."""

    async def _sleep(_delay: float) -> None:
        await _real_sleep(0)

    timers: list = []

    def _call_later(_hass, _delay, action):
        timers.append(action)
        return Mock()

    monkeypatch.setattr(coordinator_module.asyncio, "sleep", _sleep)
    monkeypatch.setattr(coordinator_module, "SETTING_CONFIRM_POLL", 0)
    monkeypatch.setattr(coordinator_module, "STOP_CONFIRM_REST_POLL", 0)
    monkeypatch.setattr(coordinator_module, "SETTING_CONFIRM_TIMEOUT", 0.05)
    monkeypatch.setattr(coordinator_module, "async_call_later", _call_later)
    return timers


def _coordinator(
    status: int | None, setting: dict | None = None
) -> tuple[ZeedaCoordinator, Charger, AsyncMock]:
    coordinator = object.__new__(ZeedaCoordinator)
    charger = Charger(
        code="Z123",
        name="Garage",
        charge_point_id=1,
        status=status,
        max_current=32,
        charge_mode="Authorized-Charge",
    )
    coordinator.hass = Mock()
    coordinator.chargers = {charger.code: charger}
    coordinator._command_lock = asyncio.Lock()
    coordinator._unsub_pending = {}
    coordinator._notify = Mock()
    coordinator._fire_unconfirmed = Mock()
    coordinator._schedule_confirm_refresh = Mock()
    coordinator._refresh_one = AsyncMock()
    coordinator._api = Mock()
    coordinator._api.get_point_setting = AsyncMock(
        return_value=setting if setting is not None else {"maxChargeCurrent": "16"}
    )
    ws = AsyncMock()
    ws.start_charge.return_value = ACK
    ws.stop_charge.return_value = ACK
    ws.set_max_current.return_value = ACK
    ws.set_charge_mode.return_value = ACK
    coordinator._get_ws = AsyncMock(return_value=ws)
    return coordinator, charger, ws


def _stops_on_refresh(coordinator: ZeedaCoordinator, charger: Charger) -> None:
    async def _refresh(*_args, **_kwargs) -> None:
        charger.status = 1  # the charger reports it stopped

    coordinator._refresh_one.side_effect = _refresh


# -- start / stop -------------------------------------------------------------
async def test_start_charge_is_on_immediately_and_ignores_stale_reads() -> None:
    coordinator, charger, ws = _coordinator(status=1)

    await coordinator.start_charge(charger.code)

    ws.start_charge.assert_awaited_once()
    assert charger.charging_on is True
    coordinator._notify.assert_called()

    # A stale "plugged in" push inside the window must not flip the switch back.
    coordinator._apply_payload(charger, "StatusNotification", {"connectorStatus": 1})
    assert charger.charging_on is True

    # The charger confirms -> pending cleared, real state drives it.
    coordinator._apply_payload(charger, "StatusNotification", {"connectorStatus": 2})
    assert charger.pending_charging is None
    assert charger.charging_on is True


async def test_unconfirmed_start_fires_event_and_reverts(fast_timers: list) -> None:
    coordinator, charger, _ws = _coordinator(status=1)

    await coordinator.start_charge(charger.code)
    fast_timers[-1](None)  # the pending window expires without confirmation

    assert charger.pending_charging is None
    assert charger.charging_on is False
    coordinator._fire_unconfirmed.assert_called_once()
    assert coordinator._fire_unconfirmed.call_args.args[1] == "start_charge"


async def test_confirmed_start_does_not_fire_event(fast_timers: list) -> None:
    coordinator, charger, _ws = _coordinator(status=1)

    await coordinator.start_charge(charger.code)
    coordinator._apply_payload(charger, "StatusNotification", {"connectorStatus": 3})
    fast_timers[-1](None)

    coordinator._fire_unconfirmed.assert_not_called()


async def test_rejected_command_raises_and_keeps_state() -> None:
    coordinator, charger, ws = _coordinator(status=1)
    ws.start_charge.side_effect = ZeedaCommandError("StartCharge rejected: no car")

    with pytest.raises(HomeAssistantError):
        await coordinator.start_charge(charger.code)

    assert charger.pending_charging is None
    assert charger.charging_on is False


# -- amperage -----------------------------------------------------------------
@pytest.mark.parametrize("status", [2, 3, 4])
async def test_set_current_while_on_stops_changes_and_restarts(status: int) -> None:
    coordinator, charger, ws = _coordinator(status=status)
    calls: list[str] = []
    ws.stop_charge.side_effect = lambda: calls.append("stop") or ACK
    ws.set_max_current.side_effect = lambda amps: calls.append(f"set{amps}") or ACK
    ws.start_charge.side_effect = lambda: calls.append("start") or ACK
    _stops_on_refresh(coordinator, charger)

    await coordinator.set_max_current(charger.code, 16)

    assert calls == ["stop", "set16", "start"]
    assert charger.max_current == 16
    assert charger.pending_max_current is None
    assert charger.charging_on is True


async def test_set_current_while_off_only_changes_current() -> None:
    coordinator, charger, ws = _coordinator(status=1)

    await coordinator.set_max_current(charger.code, 16)

    ws.set_max_current.assert_awaited_once_with(16)
    ws.stop_charge.assert_not_awaited()
    ws.start_charge.assert_not_awaited()
    assert charger.effective_max_current == 16


async def test_set_current_shows_target_while_in_progress() -> None:
    coordinator, charger, ws = _coordinator(status=1)
    seen: list[int | None] = []
    ws.set_max_current.side_effect = (
        lambda _amps: seen.append(charger.effective_max_current) or ACK
    )

    await coordinator.set_max_current(charger.code, 16)

    assert seen == [16]


async def test_current_not_applied_raises_reverts_and_fires_event() -> None:
    coordinator, charger, _ws = _coordinator(
        status=1, setting={"maxChargeCurrent": "32"}
    )

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.set_max_current(charger.code, 16)

    assert err.value.translation_key == "current_not_applied"
    assert charger.effective_max_current == 32
    coordinator._fire_unconfirmed.assert_called_once()
    assert coordinator._fire_unconfirmed.call_args.args[1] == "set_max_current"


async def test_set_current_restarts_even_if_change_is_rejected() -> None:
    coordinator, charger, ws = _coordinator(status=2)
    ws.set_max_current.side_effect = ZeedaCommandError("rejected")
    _stops_on_refresh(coordinator, charger)

    with pytest.raises(HomeAssistantError):
        await coordinator.set_max_current(charger.code, 16)

    ws.start_charge.assert_awaited_once()
    assert charger.effective_max_current == 32


async def test_set_current_aborts_if_charger_never_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "STOP_CONFIRM_TIMEOUT", 0.05)
    coordinator, charger, ws = _coordinator(status=2)

    with pytest.raises(HomeAssistantError):
        await coordinator.set_max_current(charger.code, 16)

    ws.set_max_current.assert_not_awaited()
    ws.start_charge.assert_awaited_once()
    assert charger.effective_max_current == 32


async def test_stop_wait_finishes_on_a_pushed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pushed status confirms the stop; no REST poll is needed."""
    monkeypatch.setattr(coordinator_module, "STOP_CONFIRM_REST_POLL", 3600)
    monkeypatch.setattr(coordinator_module, "STOP_CONFIRM_TIMEOUT", 2)
    coordinator, charger, _ws = _coordinator(status=2)
    refresh = AsyncMock()
    coordinator._refresh_one = refresh

    async def _push() -> None:
        await _real_sleep(0)
        charger.status = 1  # StatusNotification arrives over the socket

    push = asyncio.ensure_future(_push())
    try:
        assert await coordinator._wait_until_stopped(charger.code, charger) is True
    finally:
        await push

    refresh.assert_not_awaited()


# -- charge mode --------------------------------------------------------------
async def test_charge_mode_confirmed() -> None:
    coordinator, charger, _ws = _coordinator(
        status=1, setting={"chargeMode": "Plug-and-Charge"}
    )

    await coordinator.set_charge_mode(charger.code, "Plug-and-Charge")

    assert charger.charge_mode == "Plug-and-Charge"
    assert charger.pending_charge_mode is None


async def test_charge_mode_not_applied_raises_and_reverts() -> None:
    coordinator, charger, _ws = _coordinator(
        status=1, setting={"chargeMode": "Authorized-Charge"}
    )

    with pytest.raises(HomeAssistantError):
        await coordinator.set_charge_mode(charger.code, "Plug-and-Charge")

    assert charger.effective_charge_mode == "Authorized-Charge"
    coordinator._fire_unconfirmed.assert_called_once()
