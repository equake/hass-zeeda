"""Tests for the diagnostics download."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeeda.const import DOMAIN
from custom_components.zeeda.coordinator import Charger, ZeedaCoordinator
from custom_components.zeeda.diagnostics import async_get_config_entry_diagnostics


@pytest.mark.asyncio
async def test_diagnostics_redact_credentials_and_payloads(hass) -> None:
    coordinator = object.__new__(ZeedaCoordinator)
    coordinator.connected = True
    coordinator.chargers = {
        "Z123": Charger(
            code="Z123",
            name="Garage",
            charge_point_id=1,
            wifi_name="home-net",
            raw={"push": {"payload": {"idTag": "card-42"}}},
        )
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "username": "user@example.com",
            "password": "secret",
            "timezone": "America/Sao_Paulo",
        },
    )
    entry.add_to_hass(hass)
    entry.runtime_data = coordinator

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["entry"]["data"]["password"] == "**REDACTED**"
    assert result["entry"]["data"]["username"] == "**REDACTED**"
    assert result["chargers"]["Z123"]["raw"]["push"]["payload"]["idTag"] == "**REDACTED**"
    assert result["chargers"]["Z123"]["name"] == "Garage"
    assert result["connected"] is True
