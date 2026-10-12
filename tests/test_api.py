"""Tests for the Zeeda REST API helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.zeeda.api import (
    ZeedaApi,
    ZeedaApiError,
    ZeedaAuthError,
    ZeedaConnectionError,
    encrypt_password,
)


@pytest.mark.parametrize(
    ("synthetic_plaintext", "android_ciphertext"),
    (
        (
            "synthetic-only-1",
            "B536FFAC998609D441A6AC381EFFFFCCD35BF5E22D4E76D65CB171216868AAB4",
        ),
        (
            "invented-test-value-2",
            "2E70E8532CB29EC0E28D902EDCA139396933C88C684C12E5AB0E49B3FD741310",
        ),
        ("offline-test-ç", "929CC3C4FA899E06938A3E68CA6F986D"),
    ),
)
def test_encrypt_password_matches_android_ciphertext(
    synthetic_plaintext: str, android_ciphertext: str
) -> None:
    """Vectors use invented inputs, never credentials from a real account."""
    assert encrypt_password(synthetic_plaintext) == android_ciphertext


class _FakeResponse:
    def __init__(self, payload: Any, *, headers: dict | None = None, raises=None):
        self._payload = payload
        self.headers = headers or {}
        self._raises = raises

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self._raises:
            raise self._raises

    async def json(self, content_type: str | None = None) -> Any:
        return self._payload


def _api(hass, response: _FakeResponse) -> ZeedaApi:
    api = ZeedaApi(async_get_clientsession(hass))
    api.base_url = "https://sp-device.zdenergy.com/"
    api._http = Mock()
    api._http.post = Mock(return_value=response)
    return api


@pytest.mark.asyncio
async def test_post_returns_the_data_of_a_successful_call(hass) -> None:
    api = _api(hass, _FakeResponse({"success": True, "data": {"deviceCode": "Z1"}}))

    assert await api._post("path", "body") == {"deviceCode": "Z1"}


@pytest.mark.asyncio
async def test_post_passes_through_a_non_dict_payload(hass) -> None:
    api = _api(hass, _FakeResponse("not-an-envelope"))

    assert await api._post("path", "body") == "not-an-envelope"


@pytest.mark.asyncio
async def test_post_maps_bad_credentials_to_an_auth_error(hass) -> None:
    api = _api(
        hass,
        _FakeResponse(
            {
                "success": False,
                "code": "0001",
                "messageCode": "BUSS100001",
                "message": "account not found",
            }
        ),
    )

    with pytest.raises(ZeedaAuthError):
        await api._post("path", "body")


@pytest.mark.asyncio
async def test_post_maps_business_errors(hass) -> None:
    api = _api(
        hass, _FakeResponse({"success": False, "code": "9999", "message": "nope"})
    )

    with pytest.raises(ZeedaApiError) as err:
        await api._post("path", "body")

    assert err.value.code == "9999"


@pytest.mark.asyncio
async def test_post_wraps_transport_failures(hass) -> None:
    api = _api(hass, _FakeResponse({}, raises=aiohttp.ClientError("boom")))

    with pytest.raises(ZeedaConnectionError):
        await api._post("path", "body")


@pytest.mark.asyncio
async def test_post_adopts_a_rotated_token_from_the_header(hass) -> None:
    seen: list[str] = []
    api = _api(hass, _FakeResponse({"success": True}, headers={"Authorization": "new"}))
    api.token = "old"
    api._on_token_updated = seen.append

    await api._post("path", "body")

    assert api.token == "new"
    assert seen == ["new"]


@pytest.mark.asyncio
async def test_post_does_not_report_the_first_token_as_a_rotation(hass) -> None:
    """The login response carries the token too; that is not a rotation."""
    seen: list[str] = []
    api = _api(hass, _FakeResponse({"success": True}, headers={"Authorization": "first"}))
    api.token = None
    api._on_token_updated = seen.append

    await api._post("path", "body")

    assert api.token == "first"
    assert seen == []
