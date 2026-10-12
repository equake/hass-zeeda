"""Coordinator: one config entry = one Zeeda account.

Logs in, discovers the account's chargers, polls their status over REST, and
keeps one user WebSocket per charger for push updates and control commands.
Changing the amperage while the car is plugged in is orchestrated here
(stop -> change -> start), because the hardware only accepts a current change
while not actively charging.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .api import Session, ZeedaApi, ZeedaAuthError, ZeedaError
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_TIMEZONE,
    CURRENT_CHANGE_PAUSE,
    DEFAULT_CURRENT_OPTIONS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FULL_REFRESH_INTERVAL,
    STATUS_CHARGING,
    WS_DEBOUNCE,
)
from .ws import ZeedaWebSocket

_LOGGER = logging.getLogger(__name__)


@dataclass
class Charger:
    """Latest known state for one charge point."""

    code: str
    name: str
    charge_point_id: int
    connector_id: int = 1
    model: str | None = None
    firmware: str | None = None
    online: bool = False
    status: int | None = None
    error_code: int | None = None
    # live meter
    power_kw: float | None = None
    session_energy_kwh: float | None = None
    total_energy: float | None = None
    current_a: float | None = None
    voltage_v: float | None = None
    duration: str | None = None
    # settings
    charge_mode: str | None = None
    max_current: int | None = None
    current_options: tuple[str, ...] = DEFAULT_CURRENT_OPTIONS
    wifi_name: str | None = None
    support_solar: bool = False
    support_load_balancing: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class ZeedaCoordinator:
    """Manages one account: auth, polling, websockets and control."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._email: str = entry.data[CONF_USERNAME]
        self._password: str = entry.data[CONF_PASSWORD]
        self._timezone: str = entry.data.get(CONF_TIMEZONE) or str(hass.config.time_zone)
        self._interval = timedelta(
            seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )

        self._language = "en"
        self._api = ZeedaApi(async_get_clientsession(hass), self._language)
        self._session: Session | None = None
        self._ws: dict[str, ZeedaWebSocket] = {}
        self._listeners: set = set()
        self._unsub_poll = None
        self._command_lock = asyncio.Lock()
        self._last_full: float = 0.0
        self._ws_debounce: asyncio.TimerHandle | None = None

        self.chargers: dict[str, Charger] = {}
        self.connected: bool = False

    # -- lifecycle ------------------------------------------------------------
    async def async_setup(self) -> None:
        await self._login()
        await self._discover()
        await self.async_refresh(full=True)
        # Connect the real-time WebSocket per charger (primary live source).
        for code in self.chargers:
            try:
                await self._get_ws(code)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("WS connect failed for %s: %s", code, err)
        self._unsub_poll = async_track_time_interval(
            self.hass, self._scheduled_refresh, self._interval
        )

    async def async_shutdown(self) -> None:
        if self._unsub_poll:
            self._unsub_poll()
            self._unsub_poll = None
        if self._ws_debounce:
            self._ws_debounce.cancel()
            self._ws_debounce = None
        for ws in self._ws.values():
            await ws.close()
        self._ws.clear()

    async def _login(self) -> None:
        try:
            self._session = await self._api.login(
                self._email, self._password, self._timezone
            )
        except ZeedaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

    async def _discover(self) -> None:
        assert self._session is not None
        items = await self._api.get_user_charge(
            self._session.user_id, self._session.org_id
        )
        for item in items:
            vo = item.get("chargePointVO") or {}
            code = vo.get("chargePointCode")
            if not code:
                continue
            connectors = vo.get("connectorStatus") or [{}]
            charger = Charger(
                code=code,
                name=item.get("chargePointName") or code,
                charge_point_id=int(vo.get("id") or item.get("chargePointId") or 0),
                connector_id=int(connectors[0].get("connectorId", 1)),
                model=vo.get("chargePointTypeModel"),
                firmware=vo.get("firmwareVersion"),
            )
            # Selectable current options (e.g. 8/16/32 A) are static per model —
            # fetch once here rather than on every poll.
            try:
                options = await self._api.get_current_list(code)
                if options:
                    charger.current_options = tuple(options)
            except ZeedaError:
                pass
            self.chargers[code] = charger
        if not self.chargers:
            _LOGGER.warning("Zeeda account %s has no chargers", self._email)

    # -- polling --------------------------------------------------------------
    async def _scheduled_refresh(self, _now) -> None:
        # Keep the real-time WebSocket alive; reconnect if it dropped.
        for code in self.chargers:
            ws = self._ws.get(code)
            if ws is None or not ws.connected:
                try:
                    await self._get_ws(code)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("WS reconnect failed for %s: %s", code, err)
        full = (time.monotonic() - self._last_full) >= FULL_REFRESH_INTERVAL
        await self.async_refresh(full=full)

    async def async_refresh(self, *, full: bool = False) -> None:
        try:
            for code, charger in self.chargers.items():
                await self._refresh_one(code, charger, full=full)
            self.connected = True
            if full:
                self._last_full = time.monotonic()
        except ZeedaAuthError:
            # token expired or revoked -> re-login once, then retry next tick
            self.connected = False
            await self._login()
        except ZeedaError as err:
            self.connected = False
            _LOGGER.debug("Zeeda refresh failed: %s", err)
        self._notify()

    async def _refresh_one(self, code: str, charger: Charger, *, full: bool) -> None:
        # Live meter/status — the data that actually changes. Always fetched.
        status = await self._api.get_status_info(code, charger.connector_id)
        charger.raw["status"] = status
        charger.status = status.get("connectorStatus")
        charger.error_code = status.get("errorCode")
        meter = status.get("currentMeterValue") or {}
        charger.power_kw = meter.get("chargingPower")
        charger.session_energy_kwh = meter.get("chargingEnergyValue")
        charger.total_energy = meter.get("energyValue")
        charger.duration = meter.get("timeConsume")
        currents = meter.get("currentList") or []
        charger.current_a = currents[0].get("currentValue") if currents else None
        voltages = meter.get("voltageList") or []
        charger.voltage_v = voltages[0].get("voltageValue") if voltages else None

        if not full:
            return

        # Slow-changing config/identity — only on the periodic full refresh.
        detail = await self._api.get_point_detail(code)
        setting = await self._api.get_point_setting(code)
        charger.raw["detail"] = detail
        charger.raw["setting"] = setting
        charger.online = detail.get("onlineOfflineStatus") == 0
        charger.firmware = detail.get("firmwareVersion") or charger.firmware
        charger.wifi_name = detail.get("wifiName")
        charger.support_solar = bool(detail.get("supportSolarMode"))
        charger.support_load_balancing = bool(detail.get("supportLoadBalancing"))
        charger.charge_mode = setting.get("chargeMode")
        max_current = setting.get("maxChargeCurrent")
        charger.max_current = int(max_current) if max_current else charger.max_current

    # -- listeners ------------------------------------------------------------
    @callback
    def async_add_listener(self, update_callback):
        self._listeners.add(update_callback)

        def _remove() -> None:
            self._listeners.discard(update_callback)

        return _remove

    @callback
    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    def device_info(self, code: str) -> DeviceInfo:
        charger = self.chargers[code]
        return DeviceInfo(
            identifiers={(DOMAIN, code)},
            name=charger.name,
            manufacturer="Zeeda",
            model=charger.model or "Zeeda charger",
            sw_version=charger.firmware,
            serial_number=code,
        )

    # -- websocket ------------------------------------------------------------
    async def _get_ws(self, code: str) -> ZeedaWebSocket:
        assert self._session is not None
        ws = self._ws.get(code)
        if ws is None:
            charger = self.chargers[code]
            ws = ZeedaWebSocket(
                async_get_clientsession(self.hass),
                self._session.base_url,
                code,
                self._session.token,
                self._session.user_id,
                charger.connector_id,
                self._language,
                on_message=self._on_ws_push,
            )
            self._ws[code] = ws
        await ws.connect()
        return ws

    @callback
    def _on_ws_push(self, _data: dict) -> None:
        """A push frame arrived — coalesce bursts into one light refresh."""
        if self._ws_debounce:
            self._ws_debounce.cancel()
        self._ws_debounce = self.hass.loop.call_later(
            WS_DEBOUNCE,
            lambda: self.hass.async_create_task(self.async_refresh()),
        )

    # -- control --------------------------------------------------------------
    async def start_charge(self, code: str) -> None:
        async with self._command_lock:
            ws = await self._get_ws(code)
            await ws.start_charge()
        await self.async_refresh()

    async def stop_charge(self, code: str) -> None:
        async with self._command_lock:
            ws = await self._get_ws(code)
            await ws.stop_charge()
        await self.async_refresh()

    async def set_charge_mode(self, code: str, mode_value: str) -> None:
        async with self._command_lock:
            ws = await self._get_ws(code)
            await ws.set_charge_mode(mode_value)
        await self.async_refresh(full=True)

    async def set_max_current(self, code: str, amps: int) -> None:
        """Set amperage, pausing/resuming the session if it is actively charging.

        The charger only accepts a current change while not charging, so when a
        session is running we stop it, apply the new current, then start again —
        giving dynamic power control with the car plugged in.
        """
        charger = self.chargers[code]
        async with self._command_lock:
            ws = await self._get_ws(code)
            was_charging = charger.status == STATUS_CHARGING
            if was_charging:
                await ws.stop_charge()
                await asyncio.sleep(CURRENT_CHANGE_PAUSE)
            await ws.set_max_current(amps)
            if was_charging:
                await asyncio.sleep(CURRENT_CHANGE_PAUSE)
                await ws.start_charge()
        await self.async_refresh(full=True)
