"""Zeeda (GWM/BYD) EV charger integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONFIG_ENTRY_VERSION, DOMAIN
from .coordinator import ZeedaCoordinator

_LOGGER = logging.getLogger(__name__)

type ZeedaConfigEntry = ConfigEntry[ZeedaCoordinator]

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ZeedaConfigEntry) -> bool:
    coordinator = ZeedaCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ZeedaConfigEntry) -> None:
    """Reload when options (e.g. scan interval) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ZeedaConfigEntry) -> bool:
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: ZeedaCoordinator = entry.runtime_data
        await coordinator.async_shutdown()
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ZeedaConfigEntry) -> bool:
    """Migrate a config entry to the current version."""
    _LOGGER.debug("Migrating Zeeda entry from version %s", entry.version)
    if entry.version == CONFIG_ENTRY_VERSION:
        return True
    _LOGGER.error("Cannot migrate Zeeda config entry from version %s", entry.version)
    return False


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ZeedaConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow deleting a charger device that is no longer on the account."""
    coordinator: ZeedaCoordinator = entry.runtime_data
    codes = {
        identifier[1]
        for identifier in device_entry.identifiers
        if identifier[0] == DOMAIN
    }
    return not codes & coordinator.chargers.keys()
