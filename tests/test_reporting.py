"""Tests for background error reporting and the charging-current select."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.zeeda import coordinator as coordinator_module
from custom_components.zeeda.const import DOMAIN, EVENT_COMMAND_UNCONFIRMED
from custom_components.zeeda.coordinator import Charger, ZeedaCoordinator
from custom_components.zeeda.select import ZeedaCurrentSelect


def _coordinator(hass: HomeAssistant) -> tuple[ZeedaCoordinator, Charger]:
    coordinator = object.__new__(ZeedaCoordinator)
    charger = Charger(
        code="Z123", name="Garage", charge_point_id=1, reachable=True, online=True
    )
    coordinator.hass = hass
    coordinator.chargers = {charger.code: charger}
    coordinator._listeners = set()
    coordinator._available = {}
    coordinator._offline_since = {}
    coordinator._offline_issues = set()
    return coordinator, charger


async def test_availability_changes_logged_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator, charger = _coordinator(hass)
    caplog.set_level(logging.INFO, logger=coordinator_module.__name__)

    coordinator._notify()
    charger.reachable = False
    coordinator._notify()
    coordinator._notify()
    charger.reachable = True
    coordinator._notify()

    unavailable = [r for r in caplog.records if "is unavailable" in r.message]
    back = [r for r in caplog.records if "available again" in r.message]
    assert len(unavailable) == 1
    assert "cloud is unreachable" in unavailable[0].message
    assert len(back) == 1


async def test_offline_repair_issue_created_and_cleared(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, charger = _coordinator(hass)
    registry = ir.async_get(hass)
    issue_id = "charger_offline_Z123"

    charger.online = False
    coordinator._notify()
    assert registry.async_get_issue(DOMAIN, issue_id) is None  # not yet

    monkeypatch.setattr(coordinator_module, "OFFLINE_ISSUE_DELAY", 0)
    coordinator._notify()
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders["name"] == "Garage"

    charger.online = True
    coordinator._notify()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_cloud_outage_does_not_raise_offline_issue(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordinator_module, "OFFLINE_ISSUE_DELAY", 0)
    coordinator, charger = _coordinator(hass)
    charger.reachable = False
    charger.online = False

    coordinator._notify()

    assert ir.async_get(hass).async_get_issue(DOMAIN, "charger_offline_Z123") is None


async def test_unconfirmed_command_fires_event(hass: HomeAssistant) -> None:
    coordinator, charger = _coordinator(hass)
    events = async_capture_events(hass, EVENT_COMMAND_UNCONFIRMED)

    coordinator._fire_unconfirmed(charger, "set_max_current", value=16)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data == {
        "device_id": None,
        "charger": "Z123",
        "name": "Garage",
        "command": "set_max_current",
        "value": 16,
    }


# -- charging-current select --------------------------------------------------
def _select(max_current: int | None = 16) -> tuple[ZeedaCurrentSelect, Mock]:
    coordinator = Mock()
    coordinator.chargers = {
        "Z123": Charger(
            code="Z123",
            name="Garage",
            charge_point_id=1,
            max_current=max_current,
            current_options=("8", "16", "32"),
        )
    }
    coordinator.set_max_current = AsyncMock()
    return ZeedaCurrentSelect(coordinator, "Z123"), coordinator


def test_current_select_lists_only_accepted_values() -> None:
    select, _ = _select()
    assert select.options == ["8 A", "16 A", "32 A"]
    assert select.current_option == "16 A"


def test_current_select_unknown_value_has_no_option() -> None:
    select, _ = _select(max_current=10)
    assert select.current_option is None


async def test_current_select_sets_amps() -> None:
    select, coordinator = _select()
    await select.async_select_option("32 A")
    coordinator.set_max_current.assert_awaited_once_with("Z123", 32)


async def test_current_select_rejects_other_values() -> None:
    select, coordinator = _select()
    with pytest.raises(ServiceValidationError):
        await select.async_select_option("20 A")
    coordinator.set_max_current.assert_not_awaited()
