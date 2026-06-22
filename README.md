# Smarter Zones

Automatically open and close the individual zones of a ducted/multi-zone air
conditioner based on the temperature in each room, the unit's current mode
(heat / cool / dry / fan / off), optional conditions (e.g. a window must be
closed), and an optional manual override.

It's a Home Assistant **custom integration** with a UI setup wizard (no YAML) and
a dependency-free Lovelace card.

## Home Assistant integration

### Install

1. Copy `custom_components/smarterzones/` into your Home Assistant
   `config/custom_components/` folder (or add the repo to HACS as an
   **Integration**).
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for
   **Smarter Zones**.

### Set it up

The wizard walks you through everything:

1. **Climate device + global options** — choose the AC, an optional outdoor
   sensor, an optional common-zone switch, and whether to force the fan to Auto
   or auto-power-on from a trigger sensor.
2. **Add a zone** — name, the zone's switch, its room temperature sensor, an
   optional humidity sensor, an initial target temperature, and the
   cooling/heating offsets. Optionally add one or more conditions. Each zone
   becomes its own device.
3. Repeat for each zone, then **Finish**.

You can add, edit, or remove zones, or change settings, any time via the
integration's **Configure** button. **Edit a zone** lets you change a zone's
name, its switch, and its room sensor, and optionally replace its control
conditions; the zone keeps its identity (history and entity IDs) through the
edit. Target temperature and offsets aren't in the edit form because they're
live controls on the zone device itself — adjust them there.

### Dashboard card

SmarterZones ships a custom Lovelace card that shows a whole zone in one tidy
card: the current room temperature (and humidity, if configured), a target
temperature with +/− controls, a **Managed** toggle (SmarterZones auto-control on
or off), a **Zone** open/closed toggle for opening or closing the damper by hand,
and the zone's status. Both toggles are styled after Home Assistant's tile-card
control switch. A details button in the header opens a popup with the zone's
conditions and comfort bands and its editable temperature offsets.

The integration serves and auto-loads the card for you, so after install you can
just go to **Edit dashboard → Add card → "SmarterZones Zone"**. Pick the zone's
device and you're done. The card's options are:

- **Zone device** — which zone to show (its entities are discovered automatically).
- **Name** — optional label override (defaults to the device name).
- **Current readings label** — the small caption above the live temperature/humidity
  (defaults to "Now").
- **Status display** — how much status to show:
  - **Full** (default): a grid of projected status, conditions, and the
    cooling/heating comfort bands, below the controls.
  - **Compact**: hides that grid and instead shows the current temperature and a
    conditions icon in the header (humidity is hidden in this mode).

The details (tune) button is always shown; it opens a popup listing each
condition and its state, the cooling/heating bands, and the four temperature
offsets as live +/− controls.

If the card doesn't appear in the picker (some setups block auto-loaded
resources), add it manually under **Settings → Dashboards → Resources** as a
**JavaScript module** with the URL `/smarterzones/smarterzones-zone-card.js`, then
refresh. You can also use it in YAML:

```yaml
type: custom:smarterzones-zone-card
device: <your zone device id>
status_display: full
```

### What it creates

Setting up the integration creates a **controller device** with one **sub-device
per zone** underneath it. Optionally, the controller device can also expose an
**Air conditioner** (`climate`) entity that mirrors your Daikin unit and forwards
mode/temperature/fan/on-off changes back to it, so you can operate the AC from
the SmarterZones device without leaving it. Toggle this with "Show air
conditioner controls on the device" in setup or the options. Each zone device
owns its own entities, so you no longer need any `input_number` or
`input_boolean` helpers:

- **Target temperature** (`number`) — the desired temperature for the zone,
  adjustable from the dashboard and remembered across restarts.
- **Cooling/heating offsets** (four `number` entities, under Configuration) —
  the upper/lower cooling and heating offsets, adjustable live from the UI; the
  values you set at setup are just the starting points.
- **Smart control** (`switch`) — on means SmarterZones manages the zone; off
  hands it back to manual control. Replaces the old override `input_boolean`.
  (Shown on the card as the **Managed** toggle.)
- **Open zone** (`switch`) — manually open or close the zone's damper, handy when
  smart control is off. (Shown on the card as the **Zone** toggle.)
- **Zone open** (`binary_sensor`) — read-only, shows whether air is currently
  flowing to the zone.
- **Conditions met** (`binary_sensor`) — on when all of the zone's conditions
  are satisfied (always on if the zone has none); lists each condition's
  required vs current state as an attribute.
- **Projected status** (`sensor`) — Open/Closed the zone *would* be if the
  climate device were running, based on the current room temperature, target,
  offsets and conditions (a heat/cool band is assumed while the unit is off).
- **Cooling comfort range** (`sensor`) — desired range while cooling
  (target − cooling-lower … target + cooling-upper).
- **Heating comfort range** (`sensor`) — desired range while heating
  (target − heating-lower … target + heating-upper).
- **Current temperature** (`sensor`) — mirrors the zone's room sensor so the
  current reading is part of the zone device.
- **Current humidity** (`sensor`) — only created when a humidity sensor is
  configured for the zone; mirrors its reading.

  Each shows the range as text, with `low`/`high`/`target` attributes for use in
  templates and cards. They're separate because the cooling and heating offsets
  can differ.

The only things you point at existing entities are each zone's **switch** (the
Daikin zone switch), its **room temperature sensor**, and any **condition**
entities. Removing a zone in the options flow removes its device and entities;
stale zone devices can also be deleted from the device page.

### Auto fan speed

Enable **"Set fan speed automatically from zone demand"** and SmarterZones will
raise or lower the unit's fan speed based on how far the worst open zone is from
its target, nudged up by how many zones are open. The further from target (and
the more zones calling for air), the faster the fan; once rooms are close to
target it eases off.

It maps that demand onto your unit's own fan modes. By default it reads the
climate device's `fan_modes` and orders them slowest→fastest automatically; if
that guess is wrong for your unit, set **"Fan speeds, slowest to fastest"** to a
comma-separated list of the exact modes to use, e.g. `Low,Medium,High` or
`1,2,3,4,5`. Auto fan speed only acts while the unit is actively heating/cooling,
and it takes precedence over the simpler force-Auto option.

When you have set that fan-speed string, an **Auto fan speed** switch also
appears on the hub device so you can turn the feature on and off from the
dashboard without editing options. The switch remembers its state across
restarts and starts from the configured default. (Without the string defined the
switch is hidden, and the feature follows the options toggle only.)

A **Fan full-speed deviation** number also appears on the hub device, so you can
tune the ramp live from the dashboard rather than only at setup. It sets how many
degrees the worst open zone must be from its target to call for full fan speed
(default 4°): lower it to make the fan ramp up sooner, raise it to hold lower
speeds for longer. The value is restored across restarts, and auto fan speed
recalculates immediately whenever you change it.

A **Fan decision** sensor also appears on the hub device. Its state is the fan
speed the auto logic has chosen (or a status such as `Disabled`, `Unavailable`,
`Device auto`, or `Idle (off)` when it isn't actively driving the speed), and its
attributes show the reasoning behind it: the open-zone count, the worst room
deviation from target, the temperature and per-zone-bump fractions, the resulting
demand fraction, the ordered speed list, the selected speed and index, the unit's
current fan mode, and whether a change would be applied. In addition, an
`explanation` attribute gives a plain-language walk-through of the actual
calculation (worst room → temperature demand → multi-zone bump → total demand →
mapped speed → action), and `open_zone_details` lists every open zone with its
current temperature, target, and distance from target. It updates live whenever
zones open or close, rooms drift, the unit's mode changes, or the toggle flips.

### How a zone is decided

- **Off:** zone is closed.
- **Fan only:** zone is opened (no temperature logic).
- **On turn-on:** when the climate device switches on, any zone whose room is
  within its comfort band (and whose conditions are met) opens, so in-band rooms
  start getting air immediately rather than waiting.
- **On turn-off:** when the climate device switches off, every zone closes,
  including the common zone.

Zone switch commands are sent one at a time and retried a few times if the
controller rejects them, since some units (e.g. Daikin AirBase) drop commands
when several arrive at once.
- **Cool:** opens at the upper edge (`target + cooling-upper`), stays open while
  cooling down through the band, closes once the room drops below the lower edge
  (`target − cooling-lower`), and stays closed until it climbs back above the
  upper edge. The band acts as hysteresis, so a zone won't rapidly cycle.
- **Heat:** opens at the lower edge, stays open while heating up through the
  band, closes once the room rises above the upper edge, and stays closed until
  it drops below the lower edge again.
- **Heat/Cool (auto band):** open while the room is outside the band (needs
  heating or cooling), closed once it is comfortably inside.
- A zone with **conditions** is closed whenever any condition is not met.
- The **common zone** is kept open whenever no other zone is open, and is closed
  once it is comfortably within range while other zones are running.

### Daikin AirBase notes

This is tuned for your setup (Daikin AirBase, BRP15B61, via the official
[Daikin integration](https://www.home-assistant.io/integrations/daikin/)):

- AirBase exposes **one switch per zone** — point each zone's *Zone switch* at
  those (e.g. `switch.daikin_ac_a`).
- The Daikin climate reports modes `off`, `heat`, `cool`, `heat_cool`,
  `fan_only`; all are handled, including the `heat_cool` band described above.
- **Force auto fan** appends `/Auto` to the current fan speed (e.g. `3` → `3/Auto`).
  That suffix is configurable, and if your unit rejects the value the call is
  ignored rather than spamming errors — adjust the suffix to match your unit's
  fan mode names if needed.
- Each zone's target temperature is owned by the integration (a `number`
  entity), so you don't need to point at the Daikin per-zone climate entity or
  an `input_number` — just set the initial value when adding the zone.
