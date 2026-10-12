"""Tests for charger (re-)discovery and device removal."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.zeeda import async_remove_config_entry_device
from custom_components.zeeda.api import Session
from custom_components.zeeda.const import DOMAIN, ISSUE_NO_CHARGERS
from custom_components.zeeda.coordinator import Charger, ZeedaCoordinator


def _item(code: str, name: str = "Garage") -> dict:
    return {
        "chargePointName": name,
        "chargePointVO": {
            "chargePointCode": code,
            "id": 7,
            "connectorStatus": [{"connectorId": 1}],
            "chargePointTypeModel": "G3",
            "firmwareVersion": "1.2.3",
        },
    }


def _coordinator(hass) -> ZeedaCoordinator:
    coordinator = object.__new__(ZeedaCoordinator)
    coordinator.hass = hass
    coordinator.entry = Mock()
    coordinator._account = "user@example.com"
    coordinator._session = Session(
        base_url="https://sp-device.zdenergy.com/",
        token="token",
        user_id=1,
        org_id=1,
        expire_time=604800,
    )
    coordinator._api = Mock()
    coordinator._api.get_user_charge = AsyncMock(return_value=[])
    coordinator._api.get_current_list = AsyncMock(return_value=["8", "16", "32"])
    coordinator.chargers = {}
    coordinator._available = {}
    coordinator._offline_since = {}
    coordinator._offline_issues = set()
    coordinator._no_chargers_issue = False
    return coordinator


@pytest.mark.asyncio
async def test_discover_adds_a_charger_and_reports_the_change(hass) -> None:
    coordinator = _coordinator(hass)
    coordinator._api.get_user_charge = AsyncMock(return_value=[_item("Z1")])

    assert await coordinator._discover() is True
    assert coordinator.chargers["Z1"].name == "Garage"
    assert coordinator.chargers["Z1"].current_options == ("8", "16", "32")


@pytest.mark.asyncio
async def test_discover_is_quiet_when_nothing_changed(hass) -> None:
    coordinator = _coordinator(hass)
    coordinator._api.get_user_charge = AsyncMock(return_value=[_item("Z1")])

    assert await coordinator._discover() is True  # first time: new charger
    assert await coordinator._discover() is False  # unchanged
    assert coordinator.chargers["Z1"].name == "Garage"


@pytest.mark.asyncio
async def test_discover_drops_a_removed_charger(hass) -> None:
    coordinator = _coordinator(hass)
    coordinator.chargers = {
        "Z1": Charger(code="Z1", name="Garage", charge_point_id=1),
        "Z2": Charger(code="Z2", name="Driveway", charge_point_id=2),
    }
    coordinator._available = {"Z1": True, "Z2": True}
    coordinator._api.get_user_charge = AsyncMock(return_value=[_item("Z1")])

    assert await coordinator._discover() is True
    assert set(coordinator.chargers) == {"Z1"}
    assert "Z2" not in coordinator._available


@pytest.mark.asyncio
async def test_no_chargers_repair_issue_is_raised_and_cleared(hass) -> None:
    coordinator = _coordinator(hass)
    registry = ir.async_get(hass)

    assert await coordinator._discover() is False  # nothing to reconcile
    assert coordinator._no_chargers_issue is True
    assert registry.async_get_issue(DOMAIN, ISSUE_NO_CHARGERS) is not None

    coordinator._api.get_user_charge = AsyncMock(return_value=[_item("Z1")])
    assert await coordinator._discover() is True
    assert coordinator._no_chargers_issue is False
    assert registry.async_get_issue(DOMAIN, ISSUE_NO_CHARGERS) is None


@pytest.mark.asyncio
async def test_remove_device_only_allows_forgotten_chargers(hass) -> None:
    coordinator = _coordinator(hass)
    coordinator.chargers = {"Z1": Charger(code="Z1", name="Garage", charge_point_id=1)}
    entry = Mock(runtime_data=coordinator)

    known = Mock(identifiers={(DOMAIN, "Z1")})
    forgotten = Mock(identifiers={(DOMAIN, "Z9")})

    assert await async_remove_config_entry_device(hass, entry, known) is False
    assert await async_remove_config_entry_device(hass, entry, forgotten) is True
