# Zeeda cloud API (reverse-engineered)

Reverse-engineered from the official ZEEDA app `com.wxzd.zdjczb` 1.4.0 (Retrofit +
OkHttp + a user WebSocket). No certificate pinning, no request-body signing.

## Server resolution (required first)

The app asks a bootstrap host which regional backend to use for the device's
time zone, then switches to it. The account only exists on its regional server.

```
POST https://hwiot.shzhida.com/charge-service/app/getServerUrlByTimeZone
body: {"accessType":"android","data":"America/Sao_Paulo"}
-> data.serverUrl  (e.g. https://sp-device.zdenergy.com/)   # Brazil backend
```

All subsequent REST + WS calls go to the resolved server.

## Request envelope & auth

Every POST body is `{"accessType":"android","data":<payload>}`. `data` is an
object for some endpoints and a **bare string** for others (noted below).

Headers: `Authorization: <token>` (raw token, no "Bearer"), `Language: <iso>`,
`accessType: android`, plus a `lang` cookie. Responses wrap data as
`{code,message,messageCode,data,success}`; `success:false` with code `0001` /
`BUSS100001` = bad credentials **or unknown account** (generic message).

## Login

```
POST user-service/app/loginByEmail
data: {"account":"<email>","accountType":"email","password":"<AES-hex-UPPER>"}
```

Password = AES-128/CBC/PKCS5 of the plaintext, UPPERCASE hex:
- key `WXZhidaCX_V1.0.0`, IV `20180101Abcdefg!` (both 16 bytes, app-embedded).

Response `data`: `{token, expireTime (604800 = 7d), loginUserInfo:{userId,orgId,...}}`.
The token is also returned in the response `Authorization` header.

## Reads

| Endpoint | `data` | Returns |
|----------|--------|---------|
| `user-service/app/getUserInfo` | token (string) | user profile |
| `charge-service/app/getUserCharge` | `{userId,orgId}` | charger list |
| `charge-service/app/getChargePointDetail` | code (string) | detail (wifi, fw, flags, connectors) |
| `charge-service/charge/getChargeStatusInfo` | `{deviceCode,connectorId}` | **live status + meter** |
| `charge-service/app/chargePointSetting` | code (string) | `chargeMode`, `maxChargeCurrent`, reservation |
| `charge-service/app/currentList` | code (string) | selectable amps, e.g. `["8","16","32"]` |

`getChargeStatusInfo.data.currentMeterValue`: `chargingPower` (kW),
`chargingEnergyValue` (session kWh), `energyValue` (lifetime meter),
`timeConsume` ("HH:MM:SS"), `currentList[].currentValue` (A),
`voltageList[].voltageValue` (V).

### connectorStatus enum

`0` standby · `1` plugged/connected · `2` charging · `3`/`4` waiting to charge ·
`5` finished · `6` scheduled · `7` standby · `8` fault · `9` online · `10` offline.

`onlineOfflineStatus`: `0` = online.

## WebSocket (real-time + control)

```
wss://<resolved-host>/ws/charge-service/websocket/user/<deviceCode>
headers: Authorization, accessType: android, Language
heartbeat: 30 s
```

Outbound frame:
```json
{"accessType":"android","action":"<ACTION>","connectorId":1,"deviceCode":"<code>",
 "messageType":2,"payload":{...},"uid":<userId>,"uniqueId":"<uuid>"}
```

- **StartCharge** — `payload:{chargeProfileId:"",idTag:"",solarMode:""}`
- **StopCharge** — `payload:{}`
- **ChangeConfiguration** — `payload:{setVariableData:[{attributeValue,variable:{name}}]}`
  - `name="MaxChargeCurrent"`, value `"8"|"16"|"32"` — **only while not charging**
  - `name="ChargeMode"`, value:
    - `Authorized-Charge` — start/stop via app or RFID card ✓ confirmed live
    - `Plug-and-Charge` — plug in and charge automatically ✓ confirmed live
    - `CycleReservation-Charge` — scheduled ✓ confirmed live

### Inbound push frames

The server pushes frames on the same socket (`WebSocketRecived`:
`{accessType,action,connectorId,deviceCode,message,messageType,payload,success,uid,uniqueId}`).
Received actions and how the app parses them (both into the `Payload` class):

- **StatusNotification** — `payload.connectorStatus` (int), plus `solarErrorLevel` /
  `solarErrorType`. Connector state change.
- **MeterValues** — `payload`: `chargingPower` (**string**), `chargingEnergyValue`
  (double, session kWh), `energyValue` (double, lifetime), `currentList[].currentValue`,
  `voltageList[].voltageValue`, `timeConsume`, `uploadTimeStamp`.
- **StartCharge / StopCharge** — command acks carrying `success`.

`Payload` is the same shape as the REST `currentMeterValue` plus `connectorStatus`,
so the integration parses these frames directly (`_apply_payload`) for true
real-time updates; only acks/unknown frames fall back to a REST refresh.
