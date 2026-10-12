"""Tests for WebSocket command acks."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.zeeda.api import ZeedaCommandError
from custom_components.zeeda.ws import ZeedaWebSocket


def _socket() -> tuple[ZeedaWebSocket, list[dict]]:
    ws = ZeedaWebSocket(
        Mock(), "https://sp-device.zdenergy.com/", "Z123", "tok", 1, 1, "en"
    )
    sent: list[dict] = []
    ws.connect = AsyncMock()
    ws._ws = Mock(closed=False)
    ws._ws.send_str = AsyncMock(side_effect=lambda raw: sent.append(json.loads(raw)))
    return ws, sent


async def _reply(ws: ZeedaWebSocket, frame: dict) -> None:
    await asyncio.sleep(0)
    ws._dispatch(json.dumps(frame))


async def test_ack_matched_by_unique_id() -> None:
    ws, sent = _socket()
    task = asyncio.create_task(ws.stop_charge())
    await asyncio.sleep(0)
    await _reply(
        ws, {"action": "StopCharge", "uniqueId": sent[0]["uniqueId"], "success": True}
    )
    assert (await task)["success"] is True
    assert ws._pending == {}


async def test_ack_matched_by_action_when_unique_id_not_echoed() -> None:
    ws, _sent = _socket()
    task = asyncio.create_task(ws.start_charge())
    await asyncio.sleep(0)
    await _reply(ws, {"action": "StartCharge", "success": True})
    assert (await task)["success"] is True


async def test_rejected_ack_raises() -> None:
    ws, _sent = _socket()
    task = asyncio.create_task(ws.set_max_current(16))
    await asyncio.sleep(0)
    await _reply(
        ws, {"action": "ChangeConfiguration", "success": False, "message": "busy"}
    )
    with pytest.raises(ZeedaCommandError, match="busy"):
        await task


async def test_missing_ack_returns_none() -> None:
    ws, _sent = _socket()
    assert await ws.send_and_wait("StopCharge", timeout=0.01) is None
    assert ws._pending == {}


async def test_push_frames_do_not_resolve_commands() -> None:
    ws, _sent = _socket()
    task = asyncio.create_task(ws.send_and_wait("StartCharge", timeout=0.05))
    await asyncio.sleep(0)
    await _reply(ws, {"action": "StatusNotification", "payload": {"connectorStatus": 3}})
    assert await task is None
