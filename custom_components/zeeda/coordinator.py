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

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .api import (
    Session,
    ZeedaApi,
    ZeedaAuthError,
    ZeedaConnectionError,
    ZeedaError,
)
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_TIMEZONE,
    CURRENT_CHANGE_PAUSE,
    DEFAULT_CURRENT_OPTIONS,
    DEFAULT_SCAN_INTERVAL,
    AUTH_BACKOFF_BASE,
    AUTH_BACKOFF_MAX,
    DOMAIN,
    FULL_REFRESH_INTERVAL,
    STATUS_CHARGING,
    TOKEN_REFRESH_MARGIN,
    WS_ACTION_OFFLINE,
    WS_ACTION_ONLINE,
    WS_DEBOUNCE,
)
from .ws import ZeedaWebSocket

_LOGGER = logging.getLogger(__name__)


def _to_float(value: object) -> float | None:
    """Coerce a number or numeric string (push `chargingPower`) to float."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Charger:
    """Latest known state for one charge point."""

    code: str
    name: str
    charge_point_id: int
    connector_id: int = 1
    model: str | None = None
    firmware: str | None = None
    reachable: bool = False
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
        self._account: str = entry.data[CONF_USERNAME]
        self._password: str = entry.data[CONF_PASSWORD]
        self._timezone: str = entry.data.get(CONF_TIMEZONE) or str(hass.config.time_zone)
        self._interval = timedelta(
            seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )

        self._language = "en"
        self._api = ZeedaApi(
            async_get_clientsession(hass),
            self._language,
            on_token_updated=self._on_token_updated,
        )
        self._session: Session | None = None
        self._token_expires_at: float | None = None
        self._ws: dict[str, ZeedaWebSocket] = {}
        self._listeners: set = set()
        self._unsub_poll = None
        self._command_lock = asyncio.Lock()
        self._last_full: float = 0.0
        self._ws_debounce: asyncio.TimerHandle | None = None
        self._auth_failures: int = 0
        self._auth_cooldown_until: float = 0.0

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
                self._account, self._password, self._timezone
            )
        except ZeedaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        self._token_expires_at = (
            time.monotonic() + self._session.expire_time
            if self._session.expire_time > 0
            else None
        )
        # The new token invalidates existing sockets (they were built with the old
        # one). Drop them so the next refresh/command reconnects with the fresh token.
        await self._close_websockets()

    async def _attempt_login(self) -> None:
        """Log in with exponential backoff so a wrong password never hammers the
        login endpoint (5 consecutive failures lock the account — and the app —
        for 5 minutes)."""
        if time.monotonic() < self._auth_cooldown_until:
            raise ZeedaConnectionError("login is backing off after a failed attempt")
        try:
            await self._login()
        except (ConfigEntryAuthFailed, ZeedaError):
            self._auth_failures += 1
            self._auth_cooldown_until = time.monotonic() + min(
                AUTH_BACKOFF_BASE * 2 ** (self._auth_failures - 1), AUTH_BACKOFF_MAX
            )
            raise
        self._auth_failures = 0
        self._auth_cooldown_until = 0.0

    @callback
    def _on_token_updated(self, token: str) -> None:
        """Adopt a token rotated by the REST API and replace authenticated sockets."""
        if self._session is None:
            return
        self._session.token = token
        self._token_expires_at = (
            time.monotonic() + self._session.expire_time
            if self._session.expire_time > 0
            else None
        )
        self.hass.async_create_task(self._close_websockets())

    async def _close_websockets(self) -> None:
        websockets = list(self._ws.values())
        self._ws.clear()
        for ws in websockets:
            await ws.close()

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
            _LOGGER.warning("Zeeda account %s has no chargers", self._account)

    # -- polling --------------------------------------------------------------
    async def _scheduled_refresh(self, _now) -> None:
        try:
            if (
                self._token_expires_at is not None
                and time.monotonic()
                >= self._token_expires_at - TOKEN_REFRESH_MARGIN
            ):
                try:
                    await self._attempt_login()
                except ZeedaError as err:
                    _LOGGER.debug("Proactive Zeeda token refresh failed: %s", err)

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
        except ConfigEntryAuthFailed:
            self.connected = False
            self._notify()
            self.entry.async_start_reauth(self.hass)

    async def async_refresh(self, *, full: bool = False) -> None:
        any_ok = False
        try:
            for code, charger in self.chargers.items():
                # Isolate per charger: one charger's transient error must not
                # take the whole account offline.
                try:
                    await self._refresh_one(code, charger, full=full)
                    any_ok = True
                except ZeedaAuthError:
                    raise  # token problem is account-wide -> handled below
                except (
                    ZeedaError,
                    aiohttp.ClientError,
                    TimeoutError,
                    ValueError,
                ) as err:
                    any_ok = any_ok or charger.reachable
                    _LOGGER.debug("Zeeda refresh failed for %s: %s", code, err)
            self.connected = any_ok
            if any_ok and full:
                self._last_full = time.monotonic()
        except ZeedaAuthError:
            # token expired or revoked -> re-login once, then retry next tick
            self.connected = False
            for charger in self.chargers.values():
                charger.reachable = False
            try:
                await self._attempt_login()
            except ZeedaConnectionError:
                pass  # backing off after a failed login; don't hammer
        self._notify()

    async def _refresh_one(self, code: str, charger: Charger, *, full: bool) -> None:
        # Live meter/status — the data that actually changes. Always fetched.
        try:
            status = await self._api.get_status_info(code, charger.connector_id)
        except (ZeedaError, aiohttp.ClientError, TimeoutError, ValueError):
            charger.reachable = False
            raise
        charger.reachable = True
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
                on_message=lambda data, c=code: self._on_ws_push(c, data),
            )
            self._ws[code] = ws
        await ws.connect()
        return ws

    @callback
    def _on_ws_push(self, code: str, data: dict) -> None:
        """A push frame arrived. Parse it in place when possible (true real-time);
        otherwise fall back to a debounced REST refresh."""
        charger = self.chargers.get(code)
        action = data.get("action")
        payload = data.get("payload")
        if charger is not None:
            charger.reachable = True
            # The device pushes its cloud connectivity as Online/Offline frames
            # (connectorId 0, no meter payload) — apply directly for fast updates.
            if action in (WS_ACTION_ONLINE, WS_ACTION_OFFLINE):
                charger.online = action == WS_ACTION_ONLINE
                self.connected = any(c.reachable for c in self.chargers.values())
                self._notify()
                return
        if charger is not None and isinstance(payload, dict):
            if self._apply_payload(charger, action, payload):
                self.connected = any(c.reachable for c in self.chargers.values())
                self._notify()
                return
        # Ack (StartCharge/StopCharge) or unknown frame -> re-read status via REST.
        if self._ws_debounce:
            self._ws_debounce.cancel()
        self._ws_debounce = self.hass.loop.call_later(
            WS_DEBOUNCE,
            lambda: self.hass.async_create_task(self.async_refresh()),
        )

    @callback
    def _apply_payload(self, charger: Charger, action: str | None, payload: dict) -> bool:
        """Update a charger from a push payload. Returns True if anything changed.

        The push `payload` (app class Payload) carries the same fields as the REST
        `currentMeterValue`, plus `connectorStatus`. `chargingPower` is a string.
        """
        applied = False
        if "connectorStatus" in payload and payload["connectorStatus"] is not None:
            charger.status = payload["connectorStatus"]
            applied = True
        if "chargingPower" in payload:
            charger.power_kw = _to_float(payload.get("chargingPower"))
            applied = True
        if "chargingEnergyValue" in payload:
            charger.session_energy_kwh = payload.get("chargingEnergyValue")
            applied = True
        if "energyValue" in payload:
            charger.total_energy = payload.get("energyValue")
            applied = True
        if "timeConsume" in payload:
            charger.duration = payload.get("timeConsume")
            applied = True
        if payload.get("currentList"):
            charger.current_a = payload["currentList"][0].get("currentValue")
            applied = True
        if payload.get("voltageList"):
            charger.voltage_v = payload["voltageList"][0].get("voltageValue")
            applied = True
        if applied:
            charger.raw["push"] = {"action": action, "payload": payload}
        return applied

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
