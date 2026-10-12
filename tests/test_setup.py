"""Tests for config entry setup resilience."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeeda.api import (
    ZeedaApi,
    ZeedaApiError,
    ZeedaAuthError,
    ZeedaConnectionError,
)
from custom_components.zeeda.const import DOMAIN


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            "username": "user@example.com",
            "password": "secret",
            "timezone": "America/Sao_Paulo",
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize(
    "error",
    [
        ZeedaConnectionError("network is down"),
        aiohttp.ClientError("connection reset"),
        TimeoutError(),
    ],
)
async def test_transient_setup_failure_is_retried(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """A transient failure at boot must land in SETUP_RETRY, not SETUP_ERROR."""
    entry = _entry(hass)
    monkeypatch.setattr(ZeedaApi, "login", AsyncMock(side_effect=error))

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_rejected_credentials_start_reauth(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(hass)
    monkeypatch.setattr(
        ZeedaApi, "login", AsyncMock(side_effect=ZeedaAuthError("bad credentials"))
    )

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_refused_login_backs_off_across_setup_retries(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A login refused with an undocumented code (e.g. account lockout) must not
    be retried on every setup attempt: the backoff survives the new coordinator."""
    entry = _entry(hass)
    login = AsyncMock(side_effect=ZeedaApiError("9999", "account locked"))
    monkeypatch.setattr(ZeedaApi, "login", login)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert login.await_count == 1

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert login.await_count == 1  # still backing off: no new login attempt
