# AGENTS.md — Zeeda EV Charger integration

Read this file before making any changes.

## Workflow (required)

**Always work on a feature branch and open a PR — never commit directly to `main`.**
Branch off `main` (`fix/...`, `feat/...`), commit there, push, and open a PR with
`gh pr create`.

### PR description formatting

- Write the PR description as Markdown in a file and pass it with
  `gh pr create --body-file <path>` or `gh pr edit --body-file <path>`.
- Do not pass multiline descriptions as quoted `--body` strings or encode line
  breaks as `\n`; shell escaping can make those characters appear literally on
  GitHub.
- After creating or editing a PR, verify the rendered body with
  `gh pr view <number> --json body`.

## Quick Reference

| Item | Value |
|------|-------|
| Domain | `zeeda` |
| Device | Zeeda Energy G-series Wi-Fi charger (rebadged GWM / BYD) |
| Maker | Shanghai Zhida Technology (ZHIDATECH) |
| App | `com.wxzd.zdjczb` (ZEEDA) |
| Protocol | Zeeda cloud: REST + user WebSocket (see `docs/PROTOCOL.md`) |
| Min HA | 2026.3.0 |
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
  - The **WebSocket** is the primary real-time source (it reconnects itself,
    `RECONNECT_DELAY`; the scheduled REST poll is a second safety net). Push
    frames are parsed in
    place (`_apply_payload`): `StatusNotification` → `connectorStatus`,
    `MeterValues` → power/energy/current/voltage/duration. Only command acks
    (`StartCharge`/`StopCharge`) and unknown frames fall back to a debounced REST
    refresh.
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
  (`Authorized-Charge` = via app/card, `Plug-and-Charge` = plug in and charge,
  `CycleReservation-Charge` = scheduled; all confirmed live).
- Amperage: WS `ChangeConfiguration`, attributeName `MaxChargeCurrent`.
  **The charger only accepts a current change while not actively charging.** So
  `ZeedaCoordinator.set_max_current` orchestrates stop → wait until the charger
  reports it stopped (push or REST, `STOP_CONFIRM_TIMEOUT`) → change → start,
  **only if the session was on** (`ACTIVE_STATUSES` = charging/waiting, the same
  rule as the Charging switch). It restarts even if the change is rejected, and
  aborts (restarting) if the charger never reports stopped. If the session was
  off, only the current is changed. This is the dynamic power-control feature.
  Only the discrete values from `currentList` are accepted, so requested amps
  are snapped to the nearest selectable value (`_snap_current`; ties snap down).
- **Command acks:** every command waits for its WS ack (`ZeedaWebSocket.send_and_wait`,
  matched by `uniqueId`, else by `action`). `success:false` → `ZeedaCommandError`
  → `HomeAssistantError` (translated, `exceptions` in `strings.json`). No ack in
  `COMMAND_ACK_TIMEOUT` = unconfirmed, not an error.
- **Pending state:** the charger reports a new state seconds to ~1 min after a
  command. A confirmed command sets a pending value on the `Charger`
  (`charging_on`, `effective_max_current`, `effective_charge_mode`) shown
  immediately; stale reads that contradict it are ignored until a read confirms
  it (`Charger.reconcile`) or `PENDING_STATE_TIMEOUT` expires. Never refresh right
  after a command — schedule a confirm refresh (`CONFIRM_REFRESH_DELAY`) instead.

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

- Confirm `StartCharge`/`StopCharge`/`ChangeConfiguration` against the physical charger,
  and record the exact ack frame shape (does it echo `uniqueId`?) in `docs/PROTOCOL.md`.
- Consider mapping `connectorStatus` finer and exposing reservation times.
- Add a pytest suite (encrypt vector, payload parsing, config flow) and the CI job.
