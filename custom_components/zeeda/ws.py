"""Zeeda user WebSocket: real-time push + charge control commands.

The app opens ``wss://<host>/ws/charge-service/websocket/user/<deviceCode>`` with
the ``Authorization`` / ``accessType`` / ``Language`` headers, heartbeats every
30 s, and sends command frames (StartCharge / StopCharge / ChangeConfiguration).
We keep one connection per charger: it both receives pushed status frames and
carries outbound commands.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from urllib.parse import urlparse

import aiohttp

from .const import (
    ATTR_CHARGE_MODE,
    ATTR_MAX_CURRENT,
    RECONNECT_DELAY,
    USER_AGENT,
    WS_ACTION_CHANGE_CONFIG,
    WS_ACTION_START,
    WS_ACTION_STOP,
    WS_HEARTBEAT,
    WS_PATH,
)

_LOGGER = logging.getLogger(__name__)


def ws_url(base_url: str, device_code: str) -> str:
    """Build the user WS URL from the resolved REST base URL."""
    host = urlparse(base_url).hostname or "sp-device.zdenergy.com"
    return f"wss://{host}{WS_PATH}{device_code}"


class ZeedaWebSocket:
    """One persistent user-channel WebSocket for a single charger.

    When the socket drops on its own, it reconnects in the background (first
    attempt immediately, then every ``RECONNECT_DELAY`` seconds); ``close()``
    is the intentional shutdown and never reconnects.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        device_code: str,
        token: str,
        user_id: int,
        connector_id: int,
        language: str,
        on_message: Callable[[dict], None] | None = None,
    ) -> None:
        self._http = session
        self._url = ws_url(base_url, device_code)
        self._device_code = device_code
        self._token = token
        self._user_id = user_id
        self._connector_id = connector_id
        self._language = language
        self._on_message = on_message

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._closing = False
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def connect(self) -> None:
        async with self._lock:
            if self.connected:
                return
            headers = {
                "Authorization": self._token,
                "accessType": "android",
                "Language": self._language,
                "User-Agent": USER_AGENT,
            }
            self._ws = await self._http.ws_connect(
                self._url, headers=headers, heartbeat=WS_HEARTBEAT
            )
            if self._reader is not None and not self._reader.done():
                self._reader.cancel()
            self._reader = asyncio.create_task(self._read_loop())
            _LOGGER.debug("WS connected: %s", self._url)

    async def close(self) -> None:
        async with self._lock:
            self._closing = True
            for task in (self._reader, self._reconnect_task):
                if task:
                    task.cancel()
            self._reader = self._reconnect_task = None
            if self._ws is not None:
                await self._ws.close()
                self._ws = None

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type is aiohttp.WSMsgType.TEXT:
                    self._dispatch(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("WS read loop ended for %s: %s", self._device_code, err)
        # Reached only when the socket dropped on its own (a cancellation
        # re-raises above) -> reconnect in the background.
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Start the reconnect loop unless closing or one is already running."""
        if self._closing:
            return
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        """Reconnect immediately, retrying every ``RECONNECT_DELAY`` seconds."""
        while not self._closing and not self.connected:
            try:
                await self.connect()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "WS reconnect failed for %s: %s; retrying in %ss",
                    self._device_code,
                    err,
                    RECONNECT_DELAY,
                )
                await asyncio.sleep(RECONNECT_DELAY)

    def _dispatch(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if self._on_message:
            self._on_message(data)

    def _frame(self, action: str, payload: dict | None = None) -> str:
        return json.dumps(
            {
                "accessType": "android",
                "action": action,
                "connectorId": self._connector_id,
                "deviceCode": self._device_code,
                "messageType": 2,
                "payload": payload or {},
                "uid": self._user_id,
                "uniqueId": str(uuid.uuid4()),
            }
        )

    async def _send(self, action: str, payload: dict | None = None) -> None:
        await self.connect()
        assert self._ws is not None
        await self._ws.send_str(self._frame(action, payload))

    # -- commands -------------------------------------------------------------
    async def start_charge(self, id_tag: str = "", solar_mode: str = "") -> None:
        await self._send(
            WS_ACTION_START,
            {"chargeProfileId": "", "idTag": id_tag, "solarMode": solar_mode},
        )

    async def stop_charge(self) -> None:
        await self._send(WS_ACTION_STOP)

    async def _change_config(self, name: str, value: str) -> None:
        await self._send(
            WS_ACTION_CHANGE_CONFIG,
            {"setVariableData": [{"attributeValue": value, "variable": {"name": name}}]},
        )

    async def set_max_current(self, amps: int | str) -> None:
        await self._change_config(ATTR_MAX_CURRENT, str(amps))

    async def set_charge_mode(self, mode_value: str) -> None:
        await self._change_config(ATTR_CHARGE_MODE, mode_value)
