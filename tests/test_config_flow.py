"""Tests for the Zeeda account setup flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeeda.api import ZeedaAuthError
from custom_components.zeeda.const import CONF_TIMEZONE, DOMAIN


@pytest.mark.asyncio
async def test_user_flow_creates_entry(hass) -> None:
    user_input = {
        "username": " user@example.com ",
        "password": "secret",
        CONF_TIMEZONE: "America/Sao_Paulo",
    }
    validated_data = {
        "username": "user@example.com",
        "password": "secret",
        CONF_TIMEZONE: "America/Sao_Paulo",
    }

    with patch(
        "custom_components.zeeda.config_flow._validate",
        new=AsyncMock(return_value=validated_data),
    ) as validate:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=user_input
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "user@example.com"
    assert result["data"] == validated_data
    validate.assert_awaited_once_with(
        hass, "user@example.com", "secret", "America/Sao_Paulo"
    )


@pytest.mark.asyncio
async def test_user_flow_reports_invalid_credentials(hass) -> None:
    with patch(
        "custom_components.zeeda.config_flow._validate",
        new=AsyncMock(side_effect=ZeedaAuthError("invalid credentials")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "username": "user@example.com",
                "password": "wrong",
                CONF_TIMEZONE: "America/Sao_Paulo",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


def _options_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "username": "user@example.com",
            "password": "secret",
            CONF_TIMEZONE: "America/Sao_Paulo",
        },
    )


@pytest.mark.asyncio
async def test_options_flow_saves_the_interval_and_timezone(hass) -> None:
    entry = _options_entry()
    entry.add_to_hass(hass)
    validated = {
        "username": "user@example.com",
        "password": "secret",
        CONF_TIMEZONE: "Europe/Lisbon",
    }

    with patch(
        "custom_components.zeeda.config_flow._validate",
        new=AsyncMock(return_value=validated),
    ) as validate:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"scan_interval": 120, CONF_TIMEZONE: "Europe/Lisbon"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["scan_interval"] == 120
    assert result["data"][CONF_TIMEZONE] == "Europe/Lisbon"
    # A region change must be proven against the server before it is saved.
    validate.assert_awaited_once()


@pytest.mark.asyncio
async def test_options_flow_skips_the_login_when_the_timezone_is_unchanged(hass) -> None:
    entry = _options_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.zeeda.config_flow._validate", new=AsyncMock()
    ) as validate:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"scan_interval": 60, CONF_TIMEZONE: "America/Sao_Paulo"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    validate.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_flow_rejects_a_timezone_without_the_account(hass) -> None:
    entry = _options_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.zeeda.config_flow._validate",
        new=AsyncMock(side_effect=ZeedaAuthError("unknown account")),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"scan_interval": 60, CONF_TIMEZONE: "Asia/Tokyo"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
