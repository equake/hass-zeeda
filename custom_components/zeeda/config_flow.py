"""Config flow for the Zeeda charger integration — email/password login."""

from __future__ import annotations

import asyncio
import logging
import zoneinfo
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import ZeedaApi, ZeedaAuthError, ZeedaError
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_TIMEZONE,
    CONFIG_ENTRY_VERSION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _timezone_selector(default: str) -> SelectSelector:
    """A searchable dropdown of all IANA time zones, defaulting to HA's own.

    HA may run somewhere other than where the charger lives (e.g. in the cloud),
    and the Zeeda backend is chosen by this time zone — so it must be settable.
    """
    zones = sorted(zoneinfo.available_timezones())
    if default and default not in zones:
        zones.insert(0, default)
    return SelectSelector(
        SelectSelectorConfig(
            options=zones,
            mode=SelectSelectorMode.DROPDOWN,
            custom_value=True,
            sort=True,
        )
    )


def _schema(hass: HomeAssistant, defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    tz_default = defaults.get(CONF_TIMEZONE) or str(hass.config.time_zone)
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
            vol.Required(CONF_PASSWORD, default=""): str,
            vol.Required(CONF_TIMEZONE, default=tz_default): _timezone_selector(
                tz_default
            ),
        }
    )


async def _validate(hass, account: str, password: str, timezone: str) -> dict[str, Any]:
    api = ZeedaApi(async_get_clientsession(hass))
    await api.login(account, password, timezone)
    return {
        CONF_USERNAME: account,
        CONF_PASSWORD: password,
        CONF_TIMEZONE: timezone,
    }


class ZeedaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Email/password login flow."""

    VERSION = CONFIG_ENTRY_VERSION

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ZeedaOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            account = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            timezone = user_input[CONF_TIMEZONE]

            await self.async_set_unique_id(account.lower())
            self._abort_if_unique_id_configured()

            try:
                data = await asyncio.wait_for(
                    _validate(self.hass, account, password, timezone), timeout=30.0
                )
            except ZeedaAuthError:
                errors["base"] = "invalid_auth"
            except (ZeedaError, TimeoutError, OSError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during Zeeda setup")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=account, data=data)

        return self.async_show_form(
            step_id="user", data_schema=_schema(self.hass, user_input), errors=errors
        )

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            email = reauth_entry.data[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            timezone = reauth_entry.data.get(CONF_TIMEZONE) or str(
                self.hass.config.time_zone
            )
            try:
                data = await asyncio.wait_for(
                    _validate(self.hass, email, password, timezone), timeout=30.0
                )
            except ZeedaAuthError:
                errors["base"] = "invalid_auth"
            except (ZeedaError, TimeoutError, OSError):
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={**reauth_entry.data, **data},
                    reason="reauth_successful",
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD, default=""): str}),
            errors=errors,
            description_placeholders={"email": reauth_entry.data[CONF_USERNAME]},
        )


class ZeedaOptionsFlow(OptionsFlow):
    """Options: polling interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])},
            )
        current = int(
            self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=current
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=5,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.SLIDER,
                        )
                    )
                }
            ),
        )
