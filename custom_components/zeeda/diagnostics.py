"""Diagnostics for the Zeeda integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import ZeedaConfigEntry
from .coordinator import ZeedaCoordinator

_REDACT = {CONF_PASSWORD, CONF_USERNAME, "token", "idTag", "wifiName", "account", "accountId"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ZeedaConfigEntry
) -> dict[str, Any]:
    coordinator: ZeedaCoordinator = entry.runtime_data
    chargers = {
        code: {
            "name": c.name,
            "reachable": c.reachable,
            "status": c.status,
            "online": c.online,
            "charge_mode": c.charge_mode,
            "max_current": c.max_current,
            "current_options": list(c.current_options),
            "raw": c.raw,
        }
        for code, c in coordinator.chargers.items()
    }
    return {
        "entry": async_redact_data(entry.as_dict(), _REDACT),
        "connected": coordinator.connected,
        "chargers": async_redact_data(chargers, _REDACT),
    }
