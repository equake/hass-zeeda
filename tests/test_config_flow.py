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


def _account_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "username": "user@example.com",
            "password": "secret",
            CONF_TIMEZONE: "America/Sao_Paulo",
        },
    )


@pytest.mark.asyncio
async def test_reauth_flow_stores_the_new_password(hass) -> None:
    entry = _account_entry()
    entry.add_to_hass(hass)
    validated = {
        "username": "user@example.com",
        "password": "new-secret",
        CONF_TIMEZONE: "America/Sao_Paulo",
    }

    with patch(
        "custom_components.zeeda.config_flow._validate",
        new=AsyncMock(return_value=validated),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reauth", "entry_id": entry.entry_id},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"password": "new-secret"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new-secret"


@pytest.mark.asyncio
async def test_reauth_flow_rejects_a_wrong_password(hass) -> None:
    entry = _account_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.zeeda.config_flow._validate",
        new=AsyncMock(side_effect=ZeedaAuthError("bad credentials")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reauth", "entry_id": entry.entry_id},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"password": "wrong"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data["password"] == "secret"
