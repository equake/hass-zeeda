# AGENTS.md — Zeeda EV Charger integration

Read this file before making any changes.

## Workflow (required)

**Always work on a feature branch and open a PR — never commit directly to `main`.**
Branch off `main` (`fix/...`, `feat/...`), commit there, push, and open a PR with
`gh pr create`.

## Quick Reference

| Item | Value |
|------|-------|
| Domain | `zeeda` |
| Device | Zeeda Energy G-series Wi-Fi charger (rebadged GWM / BYD) |
| Maker | Shanghai Zhida Technology (ZHIDATECH) |
| App | `com.wxzd.zdjczb` (ZEEDA) |
| Protocol | Zeeda cloud: REST + user WebSocket (see `docs/PROTOCOL.md`) |
| Min Python | 3.12 |

## Key Files

- `custom_components/zeeda/` — integration code
- `api.py` — REST client + AES password encryption + server resolution
- `ws.py` — user WebSocket: push frames + control commands
- `coordinator.py` — auth, discovery, hybrid refresh, control orchestration
- `entity.py` — `ZeedaEntity` base (one entity ↔ one charger)
- `strings.json` — translation source of truth
- `helpers/test_client.py` — standalone API smoke test (no HA)
- `docs/PROTOCOL.md` — the reverse-engineered API

## Architecture

- **Config entry** = one Zeeda account; `type ZeedaConfigEntry = ConfigEntry[ZeedaCoordinator]`.
- Each charger (keyed by `chargePointCode`) becomes one HA device.
- **Hybrid data flow** (efficiency):
  - The **WebSocket** is the primary real-time source. A push frame triggers a
    debounced *light* refresh (`getChargeStatusInfo` only).
  - REST poll is a **fallback** (default 60 s) and the source for slow-changing
    data. A *full* refresh (`+ getChargePointDetail + chargePointSetting`) runs
    every `FULL_REFRESH_INTERVAL` (5 min) and after any config-changing command.
  - `currentList` (selectable amps) is static → fetched once at discovery.
    `getChargingDuration` is redundant with `status.currentMeterValue.timeConsume`
    → not polled.
- All platforms use `entry.runtime_data`, never `hass.data`.
- `_attr_has_entity_name = True` on every entity.

## Control

- Start / stop: WS `StartCharge` / `StopCharge`.
- Charge mode: WS `ChangeConfiguration`, attributeName `ChargeMode`
  (`Authorized-Charge` = via app/card, `CycleReservation-Charge` = scheduled;
  plug-and-charge value **unconfirmed** — `MODE_PLUG_AND_CHARGE` in `const.py`).
- Amperage: WS `ChangeConfiguration`, attributeName `MaxChargeCurrent`.
  **The charger only accepts a current change while not actively charging.** So
  `ZeedaCoordinator.set_max_current` orchestrates stop → change → start when a
  session is live (`CURRENT_CHANGE_PAUSE` between steps). This is the dynamic
  power-control feature.

## Coding Conventions

- `from __future__ import annotations` at the top of every file.
- Type aliases: `type X = Y`. Union syntax: `X | Y`.
- `EntityCategory.DIAGNOSTIC` (info) / `EntityCategory.CONFIG` (controls).
- No comments unless explaining a non-obvious API quirk.

## Internationalisation

- `strings.json` is the source of truth and MUST stay key-identical to every
  `translations/<locale>.json`. Currently `en` + `pt`. Any translatable change
  updates **every** locale file in the same commit.

## Linting

```bash
pip install ruff
ruff check custom_components/
```

## Open items / TODO

- Confirm the plug-and-charge `ChargeMode` value against the live API.
- Confirm the WebSocket **push frame** schema and parse it directly (today a push
  only triggers a REST refresh). Capture with `helpers/test_client.py --ws`.
- Consider mapping `connectorStatus` finer and exposing reservation times.
