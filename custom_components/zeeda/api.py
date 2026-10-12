"""Async REST client for the Zeeda (GWM) cloud API.

Reverse-engineered from the official ZEEDA app (``com.wxzd.zdjczb`` 1.4.0).

Flow: resolve the regional server for the account's time zone, log in with
email + AES-encrypted password, then every call carries the ``Authorization``
token and the ``{"accessType":"android","data":...}`` envelope. ``data`` is an
object for some endpoints and a bare string for others — see each method.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import (
    AES_IV,
    AES_KEY,
    BOOTSTRAP_URL,
    PATH_CHARGING_DURATION,
    PATH_CURRENT_LIST,
    PATH_LOGIN,
    PATH_POINT_DETAIL,
    PATH_POINT_SETTING,
    PATH_SERVER_BY_TZ,
    PATH_STATUS_INFO,
    PATH_USER_CHARGE,
    PATH_USER_INFO,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)


class ZeedaError(Exception):
    """Base error for the Zeeda API."""


class ZeedaAuthError(ZeedaError):
    """Login rejected (wrong credentials, or account on another server)."""


class ZeedaApiError(ZeedaError):
    """The server returned a non-success business code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def encrypt_password(plaintext: str) -> str:
    """AES-128/CBC/PKCS5 encrypt and return UPPERCASE hex, exactly like the app."""
    raw = plaintext.encode("utf-8")
    pad = 16 - (len(raw) % 16)
    raw += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_IV)).encryptor()
    return (enc.update(raw) + enc.finalize()).hex().upper()


@dataclass
class Session:
    base_url: str
    token: str
    user_id: int
    org_id: int
    expire_time: int


class ZeedaApi:
    """Stateless-ish REST client; holds the resolved server + token."""

    def __init__(self, session: aiohttp.ClientSession, language: str = "en") -> None:
        self._http = session
        self._language = language
        self.base_url: str | None = None
        self.token: str | None = None

    async def _post(self, path: str, data: Any, *, base: str | None = None) -> Any:
        base_url = base or self.base_url
        if base_url is None:
            raise ZeedaError("No base URL resolved; call resolve_server()/login() first")
        url = base_url.rstrip("/") + "/" + path.lstrip("/")
        headers = {
            "Content-Type": "application/json",
            "Language": self._language,
            "accessType": "android",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = self.token
        body = {"accessType": "android", "data": data}
        async with self._http.post(
            url, json=body, headers=headers, timeout=_TIMEOUT
        ) as resp:
            # The app also captures a token refreshed via the Authorization header.
            auth = resp.headers.get("Authorization")
            if auth:
                self.token = auth
            payload = await resp.json(content_type=None)
        if not isinstance(payload, dict):
            return payload
        if payload.get("success"):
            return payload.get("data")
        code = str(payload.get("code"))
        message = payload.get("message") or "request failed"
        # BUSS100001 == bad credentials / unknown account; others are generic.
        if code == "0001" or payload.get("messageCode") == "BUSS100001":
            raise ZeedaAuthError(message)
        raise ZeedaApiError(code, message)

    # -- bootstrap ------------------------------------------------------------
    async def resolve_server(self, timezone: str) -> str:
        """Ask the bootstrap host which backend serves this time zone."""
        data = await self._post(PATH_SERVER_BY_TZ, timezone, base=BOOTSTRAP_URL)
        server_url = None
        if isinstance(data, dict):
            server_url = data.get("serverUrl") or data.get("server_url") or data.get("url")
        elif isinstance(data, str):
            server_url = data
        if not server_url:
            raise ZeedaError(f"getServerUrlByTimeZone returned no serverUrl: {data!r}")
        self.base_url = server_url.rstrip("/") + "/"
        return self.base_url

    # -- auth -----------------------------------------------------------------
    async def login(self, email: str, password: str, timezone: str) -> Session:
        """Resolve the server, then authenticate; returns a Session."""
        await self.resolve_server(timezone)
        self.token = None
        data = await self._post(
            PATH_LOGIN,
            {
                "account": email.strip(),
                "accountType": "email",
                "password": encrypt_password(password.strip()),
            },
        )
        token = (data or {}).get("token")
        if token:
            self.token = token
        if not self.token:
            raise ZeedaAuthError("login succeeded but no token was returned")
        info = (data or {}).get("loginUserInfo") or {}
        assert self.base_url is not None
        return Session(
            base_url=self.base_url,
            token=self.token,
            user_id=int(info.get("userId", 0)),
            org_id=int(info.get("orgId", 0)),
            expire_time=int((data or {}).get("expireTime", 0)),
        )

    def restore(self, session: Session) -> None:
        """Reuse a previously obtained session (skip re-login)."""
        self.base_url = session.base_url
        self.token = session.token

    # -- reads ----------------------------------------------------------------
    async def get_user_info(self) -> dict[str, Any]:
        return await self._post(PATH_USER_INFO, self.token)

    async def get_user_charge(self, user_id: int, org_id: int) -> list[dict[str, Any]]:
        data = await self._post(PATH_USER_CHARGE, {"userId": user_id, "orgId": org_id})
        return data or []

    async def get_point_detail(self, code: str) -> dict[str, Any]:
        return await self._post(PATH_POINT_DETAIL, code)

    async def get_status_info(self, code: str, connector_id: int = 1) -> dict[str, Any]:
        return await self._post(
            PATH_STATUS_INFO, {"deviceCode": code, "connectorId": connector_id}
        )

    async def get_point_setting(self, code: str) -> dict[str, Any]:
        return await self._post(PATH_POINT_SETTING, code)

    async def get_current_list(self, code: str) -> list[str]:
        data = await self._post(PATH_CURRENT_LIST, code)
        return [str(x) for x in (data or [])]

    async def get_charging_duration(self, code: str) -> str | None:
        return await self._post(PATH_CHARGING_DURATION, code)
