"""Tests for the WebSocket drop/reconnect behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.zeeda.ws import ZeedaWebSocket


def _make_ws() -> ZeedaWebSocket:
    return ZeedaWebSocket(
        session=Mock(),
        base_url="https://sp-device.zdenergy.com/",
        device_code="Z123",
        token="token",
        user_id=1,
        connector_id=1,
        language="en",
    )


class _DroppedSocket:
    """A socket whose receive iterator ends immediately (connection dropped)."""

    closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_read_loop_ending_schedules_reconnect() -> None:
    """A socket that drops on its own must trigger the reconnect loop."""
    ws = _make_ws()
    ws._ws = _DroppedSocket()
    ws._schedule_reconnect = Mock()

    await ws._read_loop()

    ws._schedule_reconnect.assert_called_once_with()


@pytest.mark.asyncio
async def test_read_loop_does_not_reconnect_while_closing() -> None:
    ws = _make_ws()
    ws._ws = _DroppedSocket()
    ws._closing = True

    await ws._read_loop()

    assert ws._reconnect_task is None


@pytest.mark.asyncio
async def test_close_cancels_tasks_and_marks_closing() -> None:
    ws = _make_ws()
    socket = Mock()
    socket.close = AsyncMock()
    ws._ws = socket
    reader = asyncio.create_task(asyncio.sleep(3600))
    reconnect = asyncio.create_task(asyncio.sleep(3600))
    ws._reader = reader
    ws._reconnect_task = reconnect

    await ws.close()
    await asyncio.sleep(0)

    assert ws._closing is True
    assert ws._reader is None
    assert ws._reconnect_task is None
    assert reader.cancelled()
    assert reconnect.cancelled()
    socket.close.assert_awaited_once_with()
    assert ws._ws is None


@pytest.mark.asyncio
async def test_reconnect_retries_until_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.zeeda.ws.RECONNECT_DELAY", 0)
    ws = _make_ws()
    attempts: list[int] = []

    async def fake_connect() -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("still down")
        ws._ws = Mock()
        ws._ws.closed = False

    ws.connect = fake_connect  # type: ignore[method-assign]

    await ws._reconnect()

    assert len(attempts) == 3
    assert ws.connected is True


@pytest.mark.asyncio
async def test_reconnect_stops_when_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.zeeda.ws.RECONNECT_DELAY", 0)
    ws = _make_ws()
    attempts: list[int] = []

    async def fake_connect() -> None:
        attempts.append(1)

    ws.connect = fake_connect  # type: ignore[method-assign]
    ws._closing = True

    await ws._reconnect()

    assert attempts == []


def test_schedule_reconnect_skipped_while_closing() -> None:
    ws = _make_ws()
    ws._closing = True

    ws._schedule_reconnect()

    assert ws._reconnect_task is None
