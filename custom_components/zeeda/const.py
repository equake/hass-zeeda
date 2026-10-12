"""Constants for the Zeeda (GWM) charger integration."""

from __future__ import annotations

DOMAIN = "zeeda"

CONFIG_ENTRY_VERSION = 1

# --- Cloud API (reverse-engineered from the official ZEEDA app 1.4.0) --------
# The app first asks a bootstrap server which backend to use for the device's
# time zone, then talks to the returned server. The account lives on the
# regional backend (Brazil -> sp-device.zdenergy.com), not the bootstrap host.
BOOTSTRAP_URL = "https://hwiot.shzhida.com/"

# Password is AES/CBC/PKCS5 encrypted with these fixed, app-embedded key/IV and
# sent as an UPPERCASE hex string. Key and IV are exactly 16 bytes (AES-128).
AES_KEY = b"WXZhidaCX_V1.0.0"
AES_IV = b"20180101Abcdefg!"

# OkHttp default UA the app uses (no custom UA). Not validated server-side, but
# kept faithful.
USER_AGENT = "okhttp/4.11.0"

# REST paths (relative to the resolved base URL)
PATH_SERVER_BY_TZ = "charge-service/app/getServerUrlByTimeZone"
PATH_LOGIN = "user-service/app/loginByEmail"
PATH_USER_INFO = "user-service/app/getUserInfo"
PATH_USER_CHARGE = "charge-service/app/getUserCharge"
PATH_POINT_DETAIL = "charge-service/app/getChargePointDetail"
PATH_STATUS_INFO = "charge-service/charge/getChargeStatusInfo"
PATH_POINT_SETTING = "charge-service/app/chargePointSetting"
PATH_CURRENT_LIST = "charge-service/app/currentList"
PATH_CHARGING_DURATION = "charge-service/app/getChargingDuration"

# WebSocket user channel. Host is swapped to the resolved server's host; path is
# fixed. deviceCode (== chargePointCode) is appended.
WS_PATH = "/ws/charge-service/websocket/user/"

# --- WebSocket control vocabulary -------------------------------------------
WS_ACTION_START = "StartCharge"
WS_ACTION_STOP = "StopCharge"
WS_ACTION_CHANGE_CONFIG = "ChangeConfiguration"

# ChangeConfiguration attribute names
ATTR_MAX_CURRENT = "MaxChargeCurrent"
ATTR_CHARGE_MODE = "ChargeMode"

# ChargeMode values (confirmed against the live API).
MODE_AUTHORIZED = "Authorized-Charge"  # start/stop via app or RFID card
MODE_PLUG_AND_CHARGE = "Plug-and-Charge"  # plug in and charge automatically
MODE_RESERVATION = "CycleReservation-Charge"  # scheduled charging

CHARGE_MODES = {
    MODE_AUTHORIZED: "authorized",
    MODE_PLUG_AND_CHARGE: "plug_and_charge",
    MODE_RESERVATION: "reservation",
}
CHARGE_MODE_BY_SLUG = {v: k for k, v in CHARGE_MODES.items()}

# --- connectorStatus enum (ConnectorStatu.getConnectorStatusName) ------------
# Numeric status -> translation slug.
CONNECTOR_STATUS = {
    0: "standby",
    1: "connected",
    2: "charging",
    3: "waiting",
    4: "waiting",
    5: "finished",
    6: "scheduled",
    7: "standby",
    8: "fault",
    9: "online",
    10: "offline",
}
STATUS_CHARGING = 2
STATUS_WAITING = {3, 4}

# --- config / options --------------------------------------------------------
CONF_TIMEZONE = "timezone"
CONF_SCAN_INTERVAL = "scan_interval"

# The WebSocket is the primary real-time source; REST polling is a fallback and
# the source for slow-changing data. So the base poll can be relaxed.
DEFAULT_SCAN_INTERVAL = 60  # seconds (light status poll / WS fallback)
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 600

# Detail + settings change rarely; refresh them only this often (or after a
# command / on demand), not on every light tick.
FULL_REFRESH_INTERVAL = 300  # seconds

# Coalesce bursts of WebSocket push frames into a single refresh.
WS_DEBOUNCE = 1.5  # seconds

# Default max-current ceiling if the device advertises nothing useful.
DEFAULT_CURRENT_OPTIONS = ("8", "16", "32")

# Seconds to wait between StopCharge and re-applying current / StartCharge when
# changing amperage while plugged in (the hardware only accepts a current change
# while not actively charging).
CURRENT_CHANGE_PAUSE = 6.0

# Login token lifetime is 7 days (expireTime=604800); refresh well before that.
TOKEN_REFRESH_MARGIN = 3600  # seconds before expiry to re-login

WS_HEARTBEAT = 30  # seconds (matches the app)
RECONNECT_DELAY = 30  # seconds
