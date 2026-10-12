"""Tests for config entry setup resilience."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeeda import async_setup_entry
from custom_components.zeeda.api import ZeedaConnectionError
from custom_components.zeeda.const import DOMAIN
from custom_components.zeeda.coordinator import ZeedaCoordinator


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "username": "user@example.com",
            "password": "secret",
            "timezone": "America/Sao_Paulo",
        },
    )


@pytest.mark.asyncio
async def test_setup_entry_asks_for_retry_when_cloud_is_unreachable(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient failure at boot must be retried, not parked in SETUP_ERROR."""
    entry = _entry()
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        ZeedaCoordinator,
        "async_setup",
        AsyncMock(side_effect=ZeedaConnectionError("network is down")),
    )

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)


@pytest.mark.asyncio
async def test_setup_entry_keeps_auth_failure_as_auth_failure(
    hass, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        ZeedaCoordinator,
        "async_setup",
        AsyncMock(side_effect=ConfigEntryAuthFailed("bad credentials")),
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)
