# SmarterZones

A Home Assistant custom integration for ducted or multi-zone air conditioners.
It opens and closes each zone based on the actual temperature in that room, so
rooms reach the temperature set for them instead of whatever the unit's single
return-air sensor happens to read.

Everything is configured through the UI — no YAML, no helper entities — and the
integration ships with a dependency-free Lovelace card for controlling each zone
from a dashboard.

## Features

- **Per-room zoning** — each zone opens when its room needs conditioning and
  closes when it is comfortable, with an adjustable deadband so dampers do not
  rapidly cycle.
- **Conditions** — a zone can be tied to other entities (for example, a window
  sensor must read `closed`); the zone closes while any condition is not met.
- **Manual control** — every zone has a switch to take it out of automatic
  control, and another to open or close the damper by hand.
- **Automatic fan speed** — raises and lowers the unit's fan speed based on how
  far the rooms are from their targets and how many zones are calling for air.
- **Automatic target temperature** — nudges the unit's own setpoint so it keeps
  conditioning until the rooms are actually satisfied, not just the return-air
  sensor. The setpoint chosen on the unit is treated as a hard limit and is
  restored when the unit turns off.
- **Common zone support** — optionally keeps a nominated zone open whenever
  nothing else is, for units that need a minimum airflow path.
- **Auto power-on** — optionally turns the unit on to heat or cool when a
  trigger sensor crosses a configurable threshold.
- **Lovelace card** — one card per zone with tile-style toggles, target
  temperature controls, live status, and a drift-from-target bar.

## Requirements

- A climate entity for the air conditioner (for example, from the
  [Daikin integration](https://www.home-assistant.io/integrations/daikin/)).
- One `switch` entity per zone that opens and closes the damper.
- One temperature sensor per zone, located in the room.

## Installation

### HACS

1. Add this repository to HACS as a custom repository of type **Integration**.
2. Install **SmarterZones** from HACS.
3. Restart Home Assistant.

### Manual

1. Copy `custom_components/smarterzones/` into the Home Assistant
   `config/custom_components/` folder.
2. Restart Home Assistant.

## Configuration

Add the integration under **Settings → Devices & Services → Add Integration**
and search for **Smarter Zones**. The setup wizard asks for:

1. **Climate device and global options** — the air conditioner's climate
   entity, plus optional extras: an outdoor sensor, a common-zone switch,
   fan and setpoint automation, and auto power-on with a trigger sensor and
   thresholds.
2. **Zones** — one at a time: a name, the zone's damper switch, the room
   temperature sensor, an optional humidity sensor, an initial target
   temperature, and the cooling/heating offsets. Conditions can be added to
   each zone. Every zone becomes its own device.

All settings can be changed later via the integration's **Configure** button:
edit global settings, or add, edit, and remove zones. Editing a zone changes
its name, switch, sensor, or conditions while keeping its identity (entity IDs
and history). Target temperature and offsets are not part of the edit form
because they are live controls on the zone device itself.

## Devices and entities

Setup creates one **controller device** with a **sub-device per zone**.

### Controller device

- **Auto fan speed** (`switch`) — turns automatic fan-speed control on and off.
  Only created when an ordered fan-speed list is configured.
- **Fan full-speed deviation** (`number`) — how many degrees the worst room must
  be from its target to call for full fan speed (default 4).
- **Fan decision** (`sensor`) — the fan speed the automation has chosen, with
  attributes explaining the calculation in plain language.
- **Auto target temperature** (`switch`) — turns automatic setpoint control on
  and off. Always available; the configuration option only sets the default.
- **Setpoint bias** (`number`) — the maximum number of degrees the setpoint may
  be pushed past the unit's own reading at full demand (default 2).
- **Setpoint decision** (`sensor`) — the setpoint the automation has requested,
  with attributes explaining why.
- **Air conditioner** (`climate`, optional) — a mirror of the underlying
  climate entity so the unit can be operated from the controller device.
  Enabled with "Show air conditioner controls on the device".

### Zone devices

- **Target temperature** (`number`) — the desired temperature for the room,
  remembered across restarts.
- **Cooling/heating offsets** (four `number` entities) — how far the room may
  drift above and below the target before the zone switches, adjustable live.
- **Smart control** (`switch`) — on means SmarterZones manages the zone; off
  leaves it under manual control. Shown on the card as the **Managed** toggle.
- **Open zone** (`switch`) — opens or closes the damper directly, useful while
  smart control is off. Shown on the card as the **Zone** toggle.
- **Zone open** (`binary_sensor`) — whether air is currently flowing to the
  zone.
- **Conditions met** (`binary_sensor`) — on when all of the zone's conditions
  are satisfied; lists each condition's required and current state as an
  attribute.
- **Projected status** (`sensor`) — whether the zone would be open if the unit
  were running.
- **Cooling comfort range** / **Heating comfort range** (`sensor`) — the band
  the room is kept within for each mode, with `low`/`high`/`target` attributes
  for use in templates.
- **Current temperature** / **Current humidity** (`sensor`) — mirrors of the
  room sensors, so the readings live on the zone device. Humidity is only
  created when a humidity sensor is configured.

## Automatic fan speed

When **"Set fan speed automatically from zone demand"** is enabled, the fan
speed follows demand: the further the worst room is from its target — and the
more zones are open — the faster the fan. As rooms approach their targets the
fan eases off. It only acts while the unit is actively heating or cooling.

By default the speeds are read from the climate entity's `fan_modes` and
ordered slowest to fastest automatically. If that ordering is wrong for a
particular unit, set **"Fan speeds, slowest to fastest"** to a comma-separated
list (for example `Low,Medium,High` or `1,2,3,4,5`). The **Fan decision**
sensor shows the chosen speed and a step-by-step explanation of how it was
reached.

## Automatic target temperature

Many ducted units regulate against a single return-air sensor that rarely
matches the actual rooms, so the unit can stop conditioning while rooms are
still uncomfortable. When **"Adjust the unit's target temperature automatically
from room demand"** is enabled, SmarterZones biases the unit's setpoint past
its own reading — just far enough to keep it running until the room sensors are
satisfied, then eases off so it can idle.

The setpoint set on the unit is treated as the base level and as a hard limit:

- While **cooling**, the target is never set *above* the base.
- While **heating**, the target is never set *below* the base.
- The base is remembered when the unit is powered on and **restored when the
  unit is turned off** (or when the feature is switched off), so the displayed
  setpoint always returns to the level that was chosen.

The **Setpoint bias** number tunes how aggressively the setpoint is pushed, and
the **Setpoint decision** sensor explains every decision. The feature only acts
in single-setpoint heat or cool modes; heat/cool, fan-only, and dry modes are
left alone.

## How zones are decided

- **Cooling** — a zone opens when the room rises above `target + cooling upper
  offset`, stays open while it cools through the band, and closes below
  `target − cooling lower offset`. The band acts as hysteresis so dampers do
  not chatter.
- **Heating** — the mirror image: opens below the band, closes above it.
- **Heat/cool (auto)** — open while the room is outside the band in either
  direction, closed once comfortably inside.
- **Fan only / dry** — zones are simply opened.
- **Unit off** — every zone closes, including the common zone.
- **On power-on** — zones already within their band (with conditions met) open
  immediately so in-band rooms get air without waiting.
- **Conditions** — a zone with unmet conditions is closed regardless of
  temperature.
- **Common zone** — kept open whenever no other zone is open; closed once it is
  comfortable while other zones run. Optional — leave it unset for units that
  do not need a minimum airflow path.
- An unreadable room sensor fails safe: the zone is opened.

Zone switch commands are sent one at a time and retried, because some zone
controllers drop commands that arrive simultaneously.

## Lovelace card

The integration serves and registers a custom card automatically. Add it with
**Edit dashboard → Add card → "SmarterZones Zone"**, pick the zone's device,
and the card discovers all of the zone's entities by itself.

The card shows the room's current temperature (and humidity, if configured), a
target temperature stepper, a **Managed** toggle (automatic control on or off),
a **Zone** toggle (open or close the damper by hand), a status area, and a
**Drift** bar showing how far the room is from its target. A details button
opens a popup with each condition and its state, the comfort bands, and the
temperature offsets as live controls.

Options:

- **Zone device** (required) — the zone to show.
- **Name** — overrides the device name.
- **Current readings label** — the caption for the live readings row
  (default "Now").
- **Status display** — `full` shows a grid of projected status, conditions and
  comfort bands; `compact` moves the current temperature and a conditions icon
  into the header instead.
- **Show "from target" deviation bar** — toggles the Drift bar (default on).
- **Use icon labels instead of text** — swaps the row labels for icons.

Example YAML configuration:

```yaml
type: custom:smarterzones-zone-card
device: <zone device id>
status_display: full
show_deviation: true
icon_labels: false
```

If the card does not appear in the card picker, add
`/smarterzones/smarterzones-zone-card.js` manually as a **JavaScript module**
under **Settings → Dashboards → Resources**, then refresh the browser.

## Notes for Daikin AirBase units

Developed and tested against a Daikin AirBase (BRP15B61) through the official
[Daikin integration](https://www.home-assistant.io/integrations/daikin/):

- AirBase exposes one switch per zone (for example `switch.daikin_ac_a`) —
  point each zone's damper switch at those.
- The climate entity reports `off`, `heat`, `cool`, `heat_cool`, and
  `fan_only`; all are handled.
- **Force auto fan** appends a suffix (default `/Auto`) to the current fan
  mode, matching how AirBase expresses automatic fan. The suffix is
  configurable for units that name the mode differently, and rejected values
  are ignored rather than retried.
