"""Tests for control commands: pending state and the amperage orchestration."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.zeeda import coordinator as coordinator_module
from custom_components.zeeda.api import ZeedaCommandError
from custom_components.zeeda.coordinator import Charger, ZeedaCoordinator

ACK = {"success": True}
_real_sleep = asyncio.sleep


def _coordinator(status: int | None) -> tuple[ZeedaCoordinator, Charger, AsyncMock]:
    coordinator = object.__new__(ZeedaCoordinator)
    charger = Charger(
        code="Z123", name="Garage", charge_point_id=1, status=status, max_current=32
    )
    coordinator.chargers = {charger.code: charger}
    coordinator._command_lock = asyncio.Lock()
    coordinator._notify = Mock()
    coordinator._schedule_confirm_refresh = Mock()
    coordinator._refresh_one = AsyncMock()
    ws = AsyncMock()
    ws.start_charge.return_value = ACK
    ws.stop_charge.return_value = ACK
    ws.set_max_current.return_value = ACK
    ws.set_charge_mode.return_value = ACK
    coordinator._get_ws = AsyncMock(return_value=ws)
    return coordinator, charger, ws


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_delay: float) -> None:
        await _real_sleep(0)

    monkeypatch.setattr(coordinator_module.asyncio, "sleep", _sleep)
    monkeypatch.setattr(coordinator_module, "STOP_CONFIRM_POLL", 0)


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


async def test_pending_expires_and_real_state_wins() -> None:
    coordinator, charger, _ws = _coordinator(status=1)

    await coordinator.start_charge(charger.code)
    charger.pending_charging_until = time.monotonic() - 1

    assert charger.charging_on is False
    charger.reconcile()
    assert charger.pending_charging is None


async def test_rejected_command_raises_and_keeps_state() -> None:
    coordinator, charger, ws = _coordinator(status=1)
    ws.start_charge.side_effect = ZeedaCommandError("StartCharge rejected: no car")

    with pytest.raises(HomeAssistantError):
        await coordinator.start_charge(charger.code)

    assert charger.pending_charging is None
    assert charger.charging_on is False


@pytest.mark.parametrize("status", [2, 3, 4])
async def test_set_current_while_on_stops_changes_and_restarts(status: int) -> None:
    coordinator, charger, ws = _coordinator(status=status)
    calls: list[str] = []
    ws.stop_charge.side_effect = lambda: calls.append("stop") or ACK
    ws.set_max_current.side_effect = lambda amps: calls.append(f"set{amps}") or ACK
    ws.start_charge.side_effect = lambda: calls.append("start") or ACK

    async def _refresh(*_args, **_kwargs) -> None:
        charger.status = 1  # the charger reports it stopped

    coordinator._refresh_one.side_effect = _refresh

    await coordinator.set_max_current(charger.code, 16)

    assert calls == ["stop", "set16", "start"]
    assert charger.effective_max_current == 16
    assert charger.charging_on is True


async def test_set_current_while_off_only_changes_current() -> None:
    coordinator, charger, ws = _coordinator(status=1)

    await coordinator.set_max_current(charger.code, 16)

    ws.set_max_current.assert_awaited_once_with(16)
    ws.stop_charge.assert_not_awaited()
    ws.start_charge.assert_not_awaited()
    assert charger.effective_max_current == 16


async def test_set_current_restarts_even_if_change_is_rejected() -> None:
    coordinator, charger, ws = _coordinator(status=2)
    ws.set_max_current.side_effect = ZeedaCommandError("rejected")

    async def _refresh(*_args, **_kwargs) -> None:
        charger.status = 1

    coordinator._refresh_one.side_effect = _refresh

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


async def test_stale_settings_read_does_not_revert_current() -> None:
    coordinator, charger, _ws = _coordinator(status=1)

    await coordinator.set_max_current(charger.code, 16)
    # A full refresh still returning the old value keeps the pending one.
    charger.max_current = 32
    charger.reconcile(settings=True)
    assert charger.effective_max_current == 16

    charger.max_current = 16
    charger.reconcile(settings=True)
    assert charger.pending_max_current is None
    assert charger.effective_max_current == 16
