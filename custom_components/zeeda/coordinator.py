"""Coordinator: one config entry = one Zeeda account.

Logs in, discovers the account's chargers, polls their status over REST, and
keeps one user WebSocket per charger for push updates and control commands.
Changing the amperage while a session is on is orchestrated here (stop ->
wait until really stopped -> change -> start), because the hardware only
accepts a current change while not actively charging.

The charger reports a new state seconds to ~1 min after a command, so every
command holds its target as a *pending* value: entities show it immediately and
stale reads that contradict it are ignored until a read confirms it or
PENDING_STATE_TIMEOUT expires.
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
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .api import (
    Session,
    ZeedaApi,
    ZeedaAuthError,
    ZeedaCommandError,
    ZeedaConnectionError,
    ZeedaError,
)
from .const import (
    ACTIVE_STATUSES,
    AUTH_BACKOFF_BASE,
    AUTH_BACKOFF_MAX,
    CONF_SCAN_INTERVAL,
    CONF_TIMEZONE,
    CONFIRM_REFRESH_DELAY,
    CURRENT_CHANGE_SETTLE,
    DEFAULT_CURRENT_OPTIONS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_COMMAND_UNCONFIRMED,
    FULL_REFRESH_INTERVAL,
    ISSUE_CHARGER_OFFLINE,
    ISSUE_NO_CHARGERS,
    OFFLINE_ISSUE_DELAY,
    PENDING_STATE_TIMEOUT,
    SETTING_CONFIRM_TIMEOUT,
    STOP_CONFIRM_POLL,
    STOP_CONFIRM_TIMEOUT,
    TOKEN_REFRESH_MARGIN,
    WS_ACTION_OFFLINE,
    WS_ACTION_ONLINE,
    WS_DEBOUNCE,
)
from .ws import ZeedaWebSocket

_LOGGER = logging.getLogger(__name__)


def _to_float(value: object) -> float | None:
    """Coerce a number or numeric string from the cloud to float.

    The API is not type-stable: the push `chargingPower` is documented as a
    string while its siblings are doubles, and the REST meter is unspecified —
    device-class sensors need actual floats.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _snap_current(options: tuple[str, ...], amps: int) -> int:
    """Snap to the nearest selectable amperage.

    ``MaxChargeCurrent`` only accepts the discrete values advertised by
    ``currentList`` (e.g. 8/16/32 A), so any other requested value has to be
    mapped to one the hardware will take. Ties snap down (safer for power).
    """
    values = sorted({int(x) for x in options if str(x).isdigit()})
    if not values:
        return amps
    return min(values, key=lambda value: abs(value - amps))


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
    # commanded values awaiting confirmation by a read (None = nothing pending)
    pending_charging: bool | None = None
    pending_charging_until: float = 0.0
    pending_max_current: int | None = None
    pending_max_current_until: float = 0.0
    pending_charge_mode: str | None = None
    pending_charge_mode_until: float = 0.0

    @property
    def charging_on(self) -> bool:
        """Session on (charging or waiting), honouring a pending start/stop."""
        if (
            self.pending_charging is not None
            and time.monotonic() < self.pending_charging_until
        ):
            return self.pending_charging
        return self.status in ACTIVE_STATUSES

    @property
    def effective_max_current(self) -> int | None:
        if (
            self.pending_max_current is not None
            and time.monotonic() < self.pending_max_current_until
        ):
            return self.pending_max_current
        return self.max_current

    @property
    def effective_charge_mode(self) -> str | None:
        if (
            self.pending_charge_mode is not None
            and time.monotonic() < self.pending_charge_mode_until
        ):
            return self.pending_charge_mode
        return self.charge_mode

    def reconcile(self, *, settings: bool = False) -> None:
        """Drop pending values a read confirmed, or whose window expired.

        ``settings`` is True only right after reading chargePointSetting, the
        sole source that can confirm max current / charge mode. An expired
        start/stop is left to the coordinator's timer, which reports it.
        """
        now = time.monotonic()
        if (
            self.pending_charging is not None
            and (self.status in ACTIVE_STATUSES) == self.pending_charging
        ):
            self.pending_charging = None
        if self.pending_max_current is not None and (
            now >= self.pending_max_current_until
            or (settings and self.max_current == self.pending_max_current)
        ):
            self.pending_max_current = None
        if self.pending_charge_mode is not None and (
            now >= self.pending_charge_mode_until
            or (settings and self.charge_mode == self.pending_charge_mode)
        ):
            self.pending_charge_mode = None


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
        self._unsub_confirm: dict[str, Any] = {}
        self._unsub_pending: dict[str, Any] = {}
        self._available: dict[str, bool] = {}
        self._offline_since: dict[str, float] = {}
        self._offline_issues: set[str] = set()
        self._no_chargers_issue = False
        self._removal_strikes = 0
        self._last_full: float = 0.0
        self._ws_debounce: asyncio.TimerHandle | None = None
        self._auth_failures: int = 0
        self._auth_cooldown_until: float = 0.0
        self._reauth_warned: bool = False

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
        for unsub in (*self._unsub_confirm.values(), *self._unsub_pending.values()):
            unsub()
        self._unsub_confirm.clear()
        self._unsub_pending.clear()
        for code in self._offline_issues:
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_CHARGER_OFFLINE}_{code}")
        self._offline_issues.clear()
        if self._no_chargers_issue:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_CHARGERS)
            self._no_chargers_issue = False
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
        except ConfigEntryAuthFailed:
            self._auth_failures = min(self._auth_failures + 1, 5)
            self._auth_cooldown_until = time.monotonic() + min(
                AUTH_BACKOFF_BASE * 2 ** (self._auth_failures - 1), AUTH_BACKOFF_MAX
            )
            raise
        self._auth_failures = 0
        self._auth_cooldown_until = 0.0

    @callback
    def _start_reauth(self) -> None:
        """Trigger the reauth flow when credentials are rejected at runtime.

        Home Assistant ignores the call while a reauth/reconfigure flow is
        already in progress, so this is safe to repeat; the warning fires once
        per coordinator lifetime (a successful reauth reloads the entry).
        """
        if not self._reauth_warned:
            self._reauth_warned = True
            _LOGGER.warning(
                "Zeeda login for %s was rejected; re-authentication required",
                self._account,
            )
        self.entry.async_start_reauth(self.hass)

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

    async def _discover(self) -> bool:
        """Build the account's chargers at setup; returns True when the set changed.

        Runtime changes go through ``_reconcile_chargers``, which reloads the
        entry so this rebuilds devices and entities from scratch.
        """
        assert self._session is not None
        items = await self._api.get_user_charge(
            self._session.user_id, self._session.org_id
        )
        previous = set(self.chargers)
        codes: set[str] = set()
        for item in items:
            vo = item.get("chargePointVO") or {}
            code = vo.get("chargePointCode")
            if not code:
                continue
            codes.add(code)
            name = item.get("chargePointName") or code
            existing = self.chargers.get(code)
            if existing is not None:
                existing.name = name
                existing.model = vo.get("chargePointTypeModel") or existing.model
                existing.firmware = vo.get("firmwareVersion") or existing.firmware
                continue
            connectors = vo.get("connectorStatus") or [{}]
            charger = Charger(
                code=code,
                name=name,
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

        removed = set(self.chargers) - codes
        for code in removed:
            del self.chargers[code]
            self._available.pop(code, None)
            self._offline_since.pop(code, None)
        self._set_no_chargers_issue(not self.chargers)
        return codes != previous

    @callback
    def _set_no_chargers_issue(self, missing: bool) -> None:
        if missing == self._no_chargers_issue:
            return
        self._no_chargers_issue = missing
        if missing:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_NO_CHARGERS,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_NO_CHARGERS,
                translation_placeholders={"account": self._account},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_CHARGERS)

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

            # Safety net for the real-time WebSocket: sockets reconnect
            # themselves with RECONNECT_DELAY retries, but re-check here in
            # case a reconnect loop died.
            for code in self.chargers:
                ws = self._ws.get(code)
                if ws is None or not ws.connected:
                    try:
                        await self._get_ws(code)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("WS reconnect failed for %s: %s", code, err)
            full = (time.monotonic() - self._last_full) >= FULL_REFRESH_INTERVAL
            if full and await self._reconcile_chargers():
                return
            await self.async_refresh(full=full)
        except ConfigEntryAuthFailed:
            self.connected = False
            self._notify()
            self._start_reauth()

    async def _reconcile_chargers(self) -> bool:
        """Re-check the account's chargers; reload if the set really changed.

        Does not touch ``self.chargers``: entities of a removed charger still
        read it until the reload rebuilds everything. A new charger reloads at
        once; a removal (or an empty list) must be seen on two consecutive full
        refreshes, so one bad cloud answer cannot drop every entity. Skipped
        while a command runs, so a reload never cuts a stop -> change -> start.
        """
        if self._command_lock.locked():
            return False
        assert self._session is not None
        try:
            items = await self._api.get_user_charge(
                self._session.user_id, self._session.org_id
            )
        except (ZeedaError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Zeeda charger re-discovery failed: %s", err)
            return False
        codes = {
            code
            for item in items
            if (code := (item.get("chargePointVO") or {}).get("chargePointCode"))
        }
        added = codes - self.chargers.keys()
        removed = self.chargers.keys() - codes
        self._removal_strikes = self._removal_strikes + 1 if removed else 0
        if not added and (not removed or self._removal_strikes < 2):
            if removed:
                _LOGGER.debug(
                    "Zeeda charger(s) %s missing from the account; confirming",
                    ", ".join(sorted(removed)),
                )
            return False
        # Chargers were added or removed in the Zeeda app: reload so the device
        # and entity set matches instead of keeping stale ones (or missing new
        # ones) until the user reloads by hand.
        _LOGGER.info(
            "Zeeda account %s charger list changed; reloading the entry",
            self._account,
        )
        self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
        return True

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
            except ConfigEntryAuthFailed:
                # Credentials rejected at runtime: guide the user to re-auth
                # instead of letting the exception escape into the debounced
                # WS refresh task or service calls.
                self._start_reauth()
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
        charger.power_kw = _to_float(meter.get("chargingPower"))
        charger.session_energy_kwh = _to_float(meter.get("chargingEnergyValue"))
        charger.total_energy = _to_float(meter.get("energyValue"))
        charger.duration = meter.get("timeConsume")
        currents = meter.get("currentList") or []
        charger.current_a = (
            _to_float(currents[0].get("currentValue")) if currents else None
        )
        voltages = meter.get("voltageList") or []
        charger.voltage_v = (
            _to_float(voltages[0].get("voltageValue")) if voltages else None
        )

        if not full:
            charger.reconcile()
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
        charger.reconcile(settings=True)

    # -- listeners ------------------------------------------------------------
    @callback
    def async_add_listener(self, update_callback):
        self._listeners.add(update_callback)

        def _remove() -> None:
            self._listeners.discard(update_callback)

        return _remove

    @callback
    def _notify(self) -> None:
        self._track_availability()
        for update_callback in list(self._listeners):
            update_callback()

    @callback
    def _track_availability(self) -> None:
        """Log availability changes once, and manage the offline repair issue."""
        now = time.monotonic()
        for code, charger in self.chargers.items():
            available = charger.reachable and charger.online
            previous = self._available.get(code)
            self._available[code] = available
            if previous is not None and available != previous:
                if available:
                    _LOGGER.info("Zeeda charger %s is available again", charger.name)
                else:
                    _LOGGER.info(
                        "Zeeda charger %s is unavailable: %s",
                        charger.name,
                        "the charger is offline"
                        if charger.reachable
                        else "the Zeeda cloud is unreachable",
                    )
            # Only the charger being offline is user-fixable (power / Wi-Fi);
            # a cloud outage is not, and leaves `online` stale anyway.
            if not charger.reachable:
                continue
            issue_id = f"{ISSUE_CHARGER_OFFLINE}_{code}"
            if charger.online:
                self._offline_since.pop(code, None)
                if code in self._offline_issues:
                    ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                    self._offline_issues.discard(code)
                continue
            since = self._offline_since.setdefault(code, now)
            if now - since >= OFFLINE_ISSUE_DELAY and code not in self._offline_issues:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key=ISSUE_CHARGER_OFFLINE,
                    translation_placeholders={
                        "name": charger.name,
                        "minutes": str(OFFLINE_ISSUE_DELAY // 60),
                    },
                )
                self._offline_issues.add(code)

    @callback
    def _fire_unconfirmed(self, charger: Charger, command: str, **data: Any) -> None:
        """Report a command the charger never confirmed (log + event)."""
        _LOGGER.warning(
            "Zeeda charger %s did not confirm %s %s",
            charger.name,
            command,
            data or "",
        )
        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, charger.code)}
        )
        self.hass.bus.async_fire(
            EVENT_COMMAND_UNCONFIRMED,
            {
                "device_id": device.id if device else None,
                "charger": charger.code,
                "name": charger.name,
                "command": command,
                **data,
            },
        )

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
            charger.session_energy_kwh = _to_float(payload.get("chargingEnergyValue"))
            applied = True
        if "energyValue" in payload:
            charger.total_energy = _to_float(payload.get("energyValue"))
            applied = True
        if "timeConsume" in payload:
            charger.duration = payload.get("timeConsume")
            applied = True
        if payload.get("currentList"):
            charger.current_a = _to_float(payload["currentList"][0].get("currentValue"))
            applied = True
        if payload.get("voltageList"):
            charger.voltage_v = _to_float(payload["voltageList"][0].get("voltageValue"))
            applied = True
        if applied:
            charger.raw["push"] = {"action": action, "payload": payload}
            charger.reconcile()
        return applied

    # -- control --------------------------------------------------------------
    @callback
    def _schedule_confirm_refresh(self, code: str, *, full: bool) -> None:
        """Re-read state a little after a command, in case no push confirms it."""
        if unsub := self._unsub_confirm.pop(code, None):
            unsub()

        async def _refresh(_now) -> None:
            self._unsub_confirm.pop(code, None)
            await self.async_refresh(full=full)

        self._unsub_confirm[code] = async_call_later(
            self.hass, CONFIRM_REFRESH_DELAY, _refresh
        )

    async def _send(self, code: str, command: str, *args: Any) -> dict | None:
        """Send a WS command and wait for its ack, mapping failures for the UI."""
        try:
            ws = await self._get_ws(code)
            return await getattr(ws, command)(*args)
        except ZeedaCommandError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_rejected",
                translation_placeholders={"error": str(err)},
            ) from err
        except (ZeedaError, aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    @callback
    def _set_pending_charging(self, charger: Charger, on: bool) -> None:
        """Hold a start/stop as pending; report it if never confirmed."""
        code = charger.code
        charger.pending_charging = on
        charger.pending_charging_until = time.monotonic() + PENDING_STATE_TIMEOUT
        if unsub := self._unsub_pending.pop(code, None):
            unsub()

        @callback
        def _expire(_now) -> None:
            self._unsub_pending.pop(code, None)
            if charger.pending_charging is None:
                return  # confirmed meanwhile
            charger.pending_charging = None
            self._fire_unconfirmed(
                charger,
                "start_charge" if on else "stop_charge",
                timeout=PENDING_STATE_TIMEOUT,
            )
            self._notify()

        self._unsub_pending[code] = async_call_later(
            self.hass, PENDING_STATE_TIMEOUT, _expire
        )

    async def start_charge(self, code: str) -> None:
        charger = self.chargers[code]
        async with self._command_lock:
            await self._send(code, "start_charge")
            self._set_pending_charging(charger, True)
        self._notify()
        self._schedule_confirm_refresh(code, full=False)

    async def stop_charge(self, code: str) -> None:
        charger = self.chargers[code]
        async with self._command_lock:
            await self._send(code, "stop_charge")
            self._set_pending_charging(charger, False)
        self._notify()
        self._schedule_confirm_refresh(code, full=False)

    async def _confirm_setting(
        self, code: str, charger: Charger, key: str, expected: object
    ) -> bool:
        """Poll chargePointSetting until ``key`` reports ``expected``."""
        deadline = time.monotonic() + SETTING_CONFIRM_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(STOP_CONFIRM_POLL)
            try:
                setting = await self._api.get_point_setting(code)
            except (ZeedaError, aiohttp.ClientError, TimeoutError, ValueError):
                continue
            charger.raw["setting"] = setting
            if str(setting.get(key)) == str(expected):
                return True
        return False

    async def set_charge_mode(self, code: str, mode_value: str) -> None:
        """Change the charge mode; completes only once the cloud reports it."""
        charger = self.chargers[code]
        async with self._command_lock:
            charger.pending_charge_mode = mode_value
            charger.pending_charge_mode_until = time.monotonic() + PENDING_STATE_TIMEOUT
            self._notify()
            try:
                await self._send(code, "set_charge_mode", mode_value)
                if not await self._confirm_setting(
                    code, charger, "chargeMode", mode_value
                ):
                    self._fire_unconfirmed(charger, "set_charge_mode", value=mode_value)
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="charge_mode_not_applied",
                    )
                charger.charge_mode = mode_value
            finally:
                charger.pending_charge_mode = None
                self._notify()

    async def _wait_until_stopped(self, code: str, charger: Charger) -> bool:
        """Wait until the charger reports the session stopped (push or REST)."""
        deadline = time.monotonic() + STOP_CONFIRM_TIMEOUT
        next_poll = time.monotonic() + STOP_CONFIRM_POLL
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            if charger.status not in ACTIVE_STATUSES:
                return True
            if time.monotonic() >= next_poll:
                next_poll = time.monotonic() + STOP_CONFIRM_POLL
                try:
                    await self._refresh_one(code, charger, full=False)
                except (ZeedaError, aiohttp.ClientError, TimeoutError, ValueError):
                    continue
                if charger.status not in ACTIVE_STATUSES:
                    return True
        return False

    async def set_max_current(self, code: str, amps: int) -> None:
        """Set amperage, pausing/resuming the session only if it was on.

        The charger rejects a current change while charging, so when a session
        is on we stop it, wait until it really reports stopped, apply the new
        current, then start again. If it was off, only the current is changed.
        Completes only once chargePointSetting reports the new value, so a
        change the charger silently ignored surfaces as an error.
        """
        charger = self.chargers[code]
        snapped = _snap_current(charger.current_options, amps)
        if snapped != amps:
            _LOGGER.debug("Snapping %s A to selectable %s A for %s", amps, snapped, code)
        async with self._command_lock:
            was_on = charger.charging_on
            charger.pending_max_current = snapped
            charger.pending_max_current_until = time.monotonic() + PENDING_STATE_TIMEOUT
            self._notify()
            try:
                if was_on:
                    await self._send(code, "stop_charge")
                    if not await self._wait_until_stopped(code, charger):
                        # Don't leave the car stuck without charging.
                        await self._send(code, "start_charge")
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="stop_not_confirmed",
                        )
                change_error: HomeAssistantError | None = None
                try:
                    await self._send(code, "set_max_current", snapped)
                except HomeAssistantError as err:
                    change_error = err
                if was_on:
                    await asyncio.sleep(CURRENT_CHANGE_SETTLE)
                    await self._send(code, "start_charge")
                    self._set_pending_charging(charger, True)
                if change_error is not None:
                    raise change_error
                if not await self._confirm_setting(
                    code, charger, "maxChargeCurrent", snapped
                ):
                    self._fire_unconfirmed(charger, "set_max_current", value=snapped)
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="current_not_applied",
                        translation_placeholders={"amps": str(snapped)},
                    )
                charger.max_current = snapped
            finally:
                charger.pending_max_current = None
                self._notify()
