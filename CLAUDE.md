# SmarterZones — Claude Code project guide

This file orients Claude Code (and humans) to the project. Read it first.

## What this is

A Home Assistant **custom integration** that does multi-zone control of a ducted
air-conditioner by per-room temperature, plus a **dependency-free Lovelace card**
to drive each zone. It started life as the `smarterzones` AppDaemon app and was
rebuilt into a full UI-configurable integration (hub device + per-zone
sub-devices, config flow, no helper entities).

- Integration version: see `custom_components/smarterzones/manifest.json` (`2.7.0`).
- Card version: see `CARD_VERSION` in
  `custom_components/smarterzones/www/smarterzones-zone-card.js` (`1.17.5`).
- Bump both when you change the respective part (see Conventions).

## Hardware / HA context this was built against

- Unit: Daikin AirBase (BRP15B61) via the official HA Daikin integration.
- Zones are `switch.*` entities (open/close dampers).
- Fan "auto" is expressed by appending `/Auto` to a fan mode (e.g. `3` → `3/Auto`).
- HA climate modes in play: `off, heat, cool, heat_cool, fan_only` (no `dry`).
- No live HA is available in the build environment — see Validation.

## Repo layout

```
custom_components/smarterzones/      the integration
  __init__.py        setup/teardown, serves + registers the card, forwards platforms
  const.py           CONF_* keys, defaults, tuning constants
  coordinator.py     SmarterZonesManager — all control logic (event-driven)
  entity.py          shared base entity + hub_device_info()
  config_flow.py     config + options flow (settings / add / edit / remove zone)
  climate.py number.py switch.py binary_sensor.py sensor.py   platforms
  manifest.json strings.json translations/en.json
  www/smarterzones-zone-card.js      the Lovelace card (plain custom element)
brand/                               logo/icon assets
README.md                            user-facing docs
```

## Architecture

- **Hub device** (one per config entry) with optional hub entities:
  - `switch` "Auto fan speed" (RestoreEntity; only when a fan-speed list is defined)
  - `sensor` "Fan decision" (explains the auto-fan choice; see below)
  - `number` "Fan full-speed deviation" (live tuning, RestoreNumber)
  - `switch` "Auto target temperature" (RestoreEntity; always created, like the
    auto-fan switch — `CONF_AUTO_SETPOINT` only sets the default state),
    `number` "Setpoint bias" (live tuning, RestoreNumber) and `sensor`
    "Setpoint decision" (explains the auto-setpoint choice)
  - optional proxy `climate` (gated by `CONF_EXPOSE_CLIMATE`)
- **Per-zone sub-devices** (under the hub), each with:
  - `number`: Target temperature + 4 offset numbers (cooling/heating upper/lower)
  - `switch`: Smart control (RestoreEntity) + Open zone (manual damper override —
    mirrors/drives the configured `zone_switch` via `async_set_zone_damper`)
  - `binary_sensor`: Zone open (device_class opening) + Conditions met
  - `sensor`: Projected status (enum), Cooling/Heating comfort range,
    Current temperature, Current humidity (only if a humidity sensor is configured)
- **`SmarterZonesManager`** (`coordinator.py`) holds runtime state in
  `entry.runtime_data`, is event-driven via `async_track_state_change_event`, and
  notifies entities through `register_status_listener` (per zone) and
  `register_hub_listener` (hub-level, e.g. the fan-decision sensor).

## Control logic (the important bits, all in coordinator.py)

- **Zone open/close** = hysteresis with a deadband per mode:
  - Cooling: open at ≥ target+cool_upper, close at ≤ target−cool_lower, hold in band.
  - Heating: open at ≤ target−heat_lower, close at ≥ target+heat_upper, hold in band.
  - heat_cool: open outside the band, close inside.
  - On a **turn-on** (device goes from off/unavailable → an active mode — *not* a
    mode-to-mode change; this was deliberately chosen), in-band zones with
    conditions met are opened. (`_handle_climate_change`)
  - **Fail-safe**: an unreadable local sensor opens the zone.
  - **Conditions gate**: if a configured condition isn't met for the active mode,
    the zone is closed. **Debounced** (`_effective_conditions_met`): a raw condition
    change only takes effect once it has held `CONDITION_DEBOUNCE_SECONDS` (30s), so a
    flapping door sensor moves no dampers — the zone holds while a flip is pending,
    with a one-shot `async_call_later` re-check for when the window expires (a quiet
    sensor wouldn't re-trigger evaluation otherwise). Symmetric (unmet→met is also
    debounced). First evaluation adopts raw truth; the tracker is reset on unit
    turn-on and unload (`_reset_condition_tracking`). Displays (Conditions met
    sensor, card condition list, Projected status) intentionally stay raw/instant —
    only the control decision is debounced.
  - **Common zone**: keeps an airflow path open when the unit runs and nothing else
    is open. **Optional** — units that don't need a minimum airflow path (e.g. with a
    bypass damper) can leave it unset, and everything works (no damper is force-opened;
    `_decide_common` simply isn't run). It can also be *cleared* later: the options
    settings step drops any optional setting left blank (`_settings_keys()` in
    `config_flow.py`), since a cleared selector is just absent from `user_input`.
- **Decision vs application (no double-commands)**: a pass is computed then applied,
  never decided-and-applied per zone. `_async_apply_zone_decisions(zones, turn_on)`
  builds a `{switch: (open, reason) | None}` map via the pure `_decide_zone` (per-zone
  hysteresis) and `_decide_common` (airflow/comfort, the single authority for the
  common damper — its decision *overrides* the common zone's own hysteresis entry),
  then writes each switch **at most once** (`None` = hold, no command). This is what
  removed the turn-on open/close bounce: the common zone used to be opened in the zone
  loop and immediately closed by the common-zone pass. `async_manage_all` (all zones),
  `async_manage_zone` (one zone + common), and `_handle_zone_entity_change` (affected
  zones + common) all route through it. `_decide_common` judges "other zones open" from
  their **post-pass effective** state (their decision this pass, else live state), so it
  reacts to what the dampers will be, not what they are. The common zone is still
  excluded from the generic turn-on open in `_decide_zone`.
- **Projected status** sensor uses "would-be-open-if-turned-on" semantics, so it
  can differ from the live (hysteresis-held) state — that's expected.
- **Auto fan speed**: demand = worst open-zone |current−target| ÷
  `fan_full_deviation` (live, default `FAN_FULL_DEVIATION` 4.0) + per-zone bump
  (`FAN_PER_ZONE_BUMP` 0.1 × extra open zones, capped `FAN_MAX_ZONE_BUMP` 0.3),
  clamped to 1.0, mapped onto the ordered fan speeds. Only acts while actively
  conditioning. The mapping lives in `_fan_map`; `_desired_fan_mode` applies it and
  `fan_decision()` explains it (the "Fan decision" sensor's state + attributes,
  including a single-string `explanation` and `open_zone_details`).
- **Auto target temperature** (opt-in `CONF_AUTO_SETPOINT`): the unit's own
  `current_temperature` (return-air) often disagrees with the rooms, so it can
  stop conditioning early. When enabled, SmarterZones biases the unit's setpoint
  relative to *that same reading* (the value the unit regulates against) using the
  real room sensors: demand = worst open-zone unmet distance (signed toward target)
  ÷ `SETPOINT_FULL_DEVIATION` + per-zone bump, mapped to a bias between
  `SETPOINT_MIN_BIAS` and the live `setpoint_max_bias`, pushed *below* the unit's
  reading when cooling / *above* when heating; once all open zones are satisfied it
  eases the setpoint the other way (`SETPOINT_REST_MARGIN`) so the unit idles.
  **The user's base setpoint is a hard bound**: while cooling the computed target is
  never *above* the base, while heating never *below* it (`base_bounded` in
  `_setpoint_eval` — the bias only pushes past the user's level, never short of it;
  falls back to the current setpoint while the base is unknown). Clamped to the unit's
  min/max and snapped to its step; only acts in single-setpoint
  cool/heat modes (heat_cool/fan/dry/off left alone). `_setpoint_eval` computes it and
  is shared by `_async_apply_setpoint` (applies) and `setpoint_decision` (explains).
  **Base-setpoint memory**: the user's base target is (re)captured via
  `capture_setpoint_base` on a genuine **power-on from off** (in `_handle_climate_change`,
  `came_from_off = old in off/none/"" and new active` - deliberately *not* on recovery
  from unavailable/unknown, where the displayed target is the bias, not the user's
  setpoint), and also when auto-setpoint is switched on / lazily before the first bias
  (only while the base is `None`). So nothing during the operation period - neither a
  plain setpoint change (an attribute change, not a turn-on) nor a connectivity blip -
  overwrites the base stored at start-up. The base is re-instated via
  `async_restore_setpoint_base` when auto-setpoint is switched off, **and the first time
  the unit is found off** while auto-setpoint stays on (`_async_apply_setpoint`, when
  `device_mode == "off"`). That restore uses `clear=True`, so the base is then forgotten
  and SmarterZones does **not** keep writing to the unit while it stays off - it's
  re-captured on the next turn-on. The restore writes the base even when the unit is off
  (only an unavailable/unknown device is skipped); that's the point. Because some units
  **ignore writes while off**, the manager tracks `_last_commanded_setpoint` (every bias
  write) and `_last_restored_base` (every restore): if the turn-on capture finds the
  display still equal to our own last bias (and not the restored base), it keeps the
  remembered base instead of capturing the stale bias — otherwise each on/off cycle
  would corrupt the base a little further. The base is published
  as the `AutoSetpointSwitch` `base_temperature` attribute so it survives a restart while
  on. The **fan mode has the same base memory** (`capture_fan_base` on power-on / at the
  first auto-fan change, `async_restore_fan_base` on the first off pass in
  `_async_apply_fan`, the same stale-command guard via `_last_commanded_fan` /
  `_last_restored_fan`, persisted as the `AutoFanSpeedSwitch` `base_fan_mode` attribute).
  **Off-restore verification**: because off-state writes are the unreliable ones, every
  off-state restore schedules `_async_verify_restore` via `async_call_later`
  (`RESTORE_VERIFY_DELAY` 15s): it re-reads the device and re-writes the expected
  setpoint/fan if the display still shows *our own* stale value (a value matching
  neither is a user's off-state change and is left alone), retrying up to
  `RESTORE_VERIFY_ATTEMPTS` (2) before giving up with a warning. The pending check is
  cancelled on turn-on and on unload, and it no-ops if the unit is no longer off. NOTE: while actively conditioning the unit's *displayed* setpoint shows the bias,
  not the base - that's how the feature forces the unit to keep running. The real device's
  state lags writes, so reads right after a set can be stale.
- **Switch commands** use `blocking=True` with retry
  (`SWITCH_RETRY_ATTEMPTS`/`SWITCH_RETRY_DELAY`) because the Daikin drops
  simultaneous commands. **Climate writes retry the same way** via
  `_async_climate_call(service, data, description) -> bool` — every
  `set_temperature`/`set_fan_mode` (bias, restores, verify re-writes) routes
  through it; success bookkeeping (`_last_commanded_*`, verify scheduling) only
  runs when it returns True. Exception: `_async_enforce_fan` stays single-attempt,
  because a unit that rejects the `/Auto` suffix is documented as "ignored rather
  than retried" (retrying an invalid mode every pass would spam).

## The Lovelace card (`www/smarterzones-zone-card.js`)

- Plain `HTMLElement` custom element — **no** Lit/CDN/build step. Keep it dependency-free.
- The integration auto-serves it at `/smarterzones/smarterzones-zone-card.js` and
  tries to register it, but the reliable install is to add it as a Lovelace
  **Resource** (Settings → Dashboards → Resources, JavaScript Module).
- It **auto-discovers** a zone's entities from the configured `device` by
  structural classification (number/switch/binary_sensor/sensor + device_class +
  name), so it doesn't hard-code entity IDs. The two zone switches are told apart
  by category: the **config** switch is Smart control, the primary switch is the
  manual **Open zone** damper override (`this._ids.smart` vs `this._ids.openZone`).
- Editor options: `device` (required), `name`, `current_label`, `status_display`,
  `show_deviation` (boolean, default on — the "from target" bar), `icon_labels`
  (boolean, default off — swap the field labels for icons: Managed=`mdi:thermostat-auto`,
  Zone=`mdi:air-filter`, Now=`mdi:thermometer`, Target=`mdi:target`,
  Drift=`mdi:arrow-left-right`, via the `flabel()` helper; the icon column shrinks to
  the icon (`width: auto`) so it sits tight to the control with the normal field gap,
  not the wide text column).
  `status_display` is a single select (`full` | `compact`) that replaced the confusing
  `show_status` + `compact_status` boolean pair — `_statusMode()` / the editor's
  `setConfig` migrate those legacy keys (and the removed `hidden` value → `compact`,
  and the removed `show_details` toggle) for existing dashboards. The details (tune)
  button is always shown. (No `show_offsets`: offsets were editable both inline and in
  the details popup — a duplicate action — so the inline copy was dropped.) The details
  popup shows **Status** (Conditions, Cooling/Heating band) + **Temperature offsets**
  (the four offset steppers). Under the Conditions row it lists each condition with its
  state and an ok/alert icon (from the "Conditions met" binary sensor's `conditions`
  attribute) so it's clear which one isn't met.
- Layout notes from iteration: header (title + details button — no open/closed pill;
  the Zone toggle reflects that state), then **Managed** (the smart-control switch;
  card label shortened from "Smart control") and **Zone** as
  `.ctrl-switch` toggles — a self-contained **replica of HA's `ha-control-switch`**
  (the tile-card toggle): a `.cs-bg` track layer at `--control-switch-background-opacity`
  (0.2) plus a `.cs-thumb` (40% of the bar wide, icon inside) that slides off→on
  via `translateX(0)`↔`translateX(150%)` — at 40% width it travels 1.5 of its own
  widths to reach the far edge — (transform + colour, 180ms ease-in-out). Same HA
  tokens/structure: `--control-switch-on-color` (=`--primary-color`),
  `--control-switch-off-color` (=`--disabled-color`), `--control-switch-padding` (0 so
  the thumb fills the whole channel; thumb radius = border-radius − padding),
  `--control-switch-thickness` (42px).
  Binary (no text labels) like the real switch: state is shown by thumb position +
  icon (smart `mdi:power`/`mdi:power-off`; zone `mdi:air-filter`).
  `_update` toggles `.checked`/`.disabled` and `aria-checked`; click + keyboard
  (Enter/Space toggle, arrows/Home/End set a side) drive `_toggleSwitch`/`_setSwitch`
  (smart→`this._ids.smart`, zone→`this._ids.openZone`). No initial slide on load:
  `_build` then `_update` run in one synchronous pass before paint, so the thumb's
  first rendered position is the final one; only later state changes animate. Then
  **Now** and **Target** — all four are a fixed-width label to the left of a grey panel;
  Now content is centered, Target is a spread `−  value °C  +` stepper. An optional
  **Drift** (from-target) deviation bar (label `Drift`; editor toggle `show_deviation`,
  default on; shown in both full and compact modes when a room temp sensor exists) sits at
  the **bottom of the card**
  (after the status grid): centre = target, fill grows right (warmer) / left (cooler) over
  a ±3° scale (`DEV_SCALE` in `_update`) with a `+/−°` delta; the inner edge at the target is
  square (`.dev-fill.warm`/`.cool` round only the outer end). The Zone control
  drives the Open-zone switch directly, so a room can be opened/closed by hand (handy
  when Smart control is off). In **compact** `status_display` the current temperature
  moves up into the header status area (a `.head-temp` chip), humidity is dropped, and
  the separate "Now" bar is hidden (`showHeadTemp`/`showNow` in `_build`). Status grid
  below (or a compact conditions icon in compact mode). Icons are MDI glyphs (fixed weight) lightened via outline variants
  + reduced opacity. Colors use HA theme vars (`--primary-text-color`,
  `--secondary-text-color`, etc.) — a climate mode-color feature was tried and
  removed; keep to theme colors.
- The card logs a `SMARTERZONES-ZONE-CARD <version>` banner; bump `CARD_VERSION`
  on every card change so a hard-refresh is verifiable.

## Conventions

- **Versioning**: card change → bump `CARD_VERSION`; integration change → bump
  `manifest.json` `version`. Keep them independent.
- **Prose, not bullets, in code comments where it aids clarity**; match the existing
  style (docstrings on logic-heavy methods explaining *why*).
- **Theme colors only** in the card; no fixed reds/greens or mode-accent colors.
- **Keep the card dependency-free.**
- Don't reintroduce a `dry` HVAC mode; this unit doesn't expose one.

## Validation (no live HA here)

Run these after changes (all work without Home Assistant installed):

```bash
# Python: byte-compile every module
python3 -m py_compile custom_components/smarterzones/*.py

# JSON sanity
python3 -c "import json;[json.load(open('custom_components/smarterzones/'+f)) for f in ['manifest.json','strings.json','translations/en.json']]"

# Card syntax
node --check custom_components/smarterzones/www/smarterzones-zone-card.js
```

A heavier **stub-import harness** was used during development to import the modules
without HA: copy `custom_components/smarterzones` next to a folder of stub HA
modules and run with `PYTHONPATH` pointing at the stubs. The real validation is a
**test-load in a Home Assistant instance** — do that for anything behavioural.
Logic that can be unit-tested in isolation (e.g. `fan_decision`, `_fan_map`,
turn-on detection) is best checked by constructing a fake `self` and calling the
unbound method, which is how the fan logic was verified.

## After you change things (operational reminders)

- **Integration (Python) change** → reload the integration (or restart HA), not just
  a browser refresh.
- **Card change** → hard-refresh the browser and confirm the new `CARD_VERSION`
  banner in the console.

## Packaging

Distributables are built by zipping `custom_components` (and optionally `brand` +
`README.md`). Example:

```bash
zip -r smarterzones_integration.zip custom_components -x '*__pycache__*'
zip -r smarterzones_package.zip custom_components brand README.md -x '*__pycache__*'
```

## Suggested next steps / open ideas

- Add a `tests/` package with the fake-`self` unit tests for the fan logic and
  turn-on detection so they're version-controlled and runnable in CI.
- Consider a HACS release (`hacs.json` is included; verify the structure).
- Optional: an integration-level diagnostics dump for support.
