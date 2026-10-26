"""Tests for the control logic in coordinator.py (and the config-flow checks).

No Home Assistant needed: just enough of its modules are stubbed below to
import the integration, and each method is called on a fake ``self``.

    python tests/test_coordinator.py
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys
import time
import types
from enum import Enum

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)  # the code under test logs its warnings


# ------------------------------------------------------------ HA stubs


def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class _Anything:
    """Stands in for any selector class: accepts and ignores all arguments."""

    def __init__(self, *args, **kwargs):
        pass


class _FlowBase:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    def async_show_form(self, **kw):
        return {"type": "form", **kw}

    def async_show_menu(self, **kw):
        return {"type": "menu", **kw}

    def async_create_entry(self, **kw):
        return {"type": "create_entry", **kw}

    def async_abort(self, **kw):
        return {"type": "abort", **kw}

    def add_suggested_values_to_schema(self, schema, values):
        return schema


class _Schema:
    def __init__(self, schema):
        self.schema = schema

    def extend(self, more):
        return _Schema({**self.schema, **more})


class _Marker:
    def __init__(self, key, default=None):
        self.schema = key


class _Platform(str, Enum):
    CLIMATE = "climate"
    NUMBER = "number"
    SWITCH = "switch"
    BINARY_SENSOR = "binary_sensor"
    SENSOR = "sensor"


class _ConfigEntry:
    def __class_getitem__(cls, item):
        return cls


_module("homeassistant")
_module("homeassistant.config_entries", ConfigEntry=_ConfigEntry,
        ConfigFlow=_FlowBase, OptionsFlow=_FlowBase, ConfigFlowResult=dict)
_module("homeassistant.core", HomeAssistant=object, EventStateChangedData=dict,
        Event=_ConfigEntry, callback=lambda fn: fn)
_module("homeassistant.const", Platform=_Platform, PERCENTAGE="%",
        EntityCategory=types.SimpleNamespace(CONFIG="config"),
        UnitOfTemperature=types.SimpleNamespace(CELSIUS="°C"))
_module("homeassistant.helpers")
_module("homeassistant.helpers.device_registry", DeviceInfo=dict,
        DeviceEntry=object, async_get=lambda hass: None)
_module("homeassistant.helpers.entity", Entity=object)
_module("homeassistant.helpers.event",
        async_track_state_change_event=lambda *a: (lambda: None),
        async_call_later=lambda *a: (lambda: None))
_selector = _module("homeassistant.helpers.selector")
_selector.__getattr__ = lambda name: _Anything
_module("homeassistant.components")
_module("homeassistant.components.http", StaticPathConfig=_Anything)
_module("voluptuous", Schema=_Schema, Optional=_Marker, Required=_Marker,
        UNDEFINED=object())

from custom_components.smarterzones import config_flow as CF  # noqa: E402
from custom_components.smarterzones import coordinator as C  # noqa: E402

M = C.SmarterZonesManager

# ------------------------------------------------------------ test helpers

failures = 0


def check(label, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"  ({detail})"))


class State:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


class Services:
    """Records service calls; optionally fails the first N or adds a delay."""

    def __init__(self, fail_first=0, delay=0.0):
        self.calls = []
        self.fail_first = fail_first
        self.delay = delay

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, dict(data)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if len(self.calls) <= self.fail_first:
            raise RuntimeError("simulated dropped command")


METHODS = [
    "_setpoint_eval", "_setpoint_demand", "_read_float", "get_zone_target",
    "capture_setpoint_base", "async_restore_setpoint_base", "_async_apply_setpoint",
    "_decide_zone", "_decide_common", "_is_ready", "is_zone_enabled",
    "_conditions_met", "_offsets", "get_zone_offset", "_device_state", "_fan_map",
    "capture_fan_base", "async_restore_fan_base", "_async_apply_fan",
    "_schedule_restore_verify", "_cancel_restore_verify", "_async_verify_restore",
    "_async_check_restore", "_async_climate_call", "_fan_demand", "_fan_open_zones",
    "_desired_fan_mode", "_ordered_fan_speeds", "_async_apply_fan_speed",
    "_effective_conditions_met", "_cancel_condition_timer", "_reset_condition_tracking",
    "get_zone", "_handle_trigger_change", "_handle_zone_entity_change",
    "async_manage_all", "async_manage_zone", "_async_apply_zone_decisions",
    "_async_set_switch", "_notify_status", "set_zone_offset", "set_zone_target",
    "async_stop_auto_fan", "async_stop_auto_setpoint", "set_auto_fan_speed",
    "set_auto_setpoint",
]


def manager(states, zones=(), targets=None, base=None, common=None, conf=None):
    """A fake manager: real methods bound to a plain namespace."""
    f = types.SimpleNamespace()
    f.hass = types.SimpleNamespace(
        states=types.SimpleNamespace(get=states.get),
        services=Services(),
        config=types.SimpleNamespace(units=types.SimpleNamespace(temperature_unit="°C")),
    )
    zones = list(zones)
    f.climate_device = "climate.ac"
    f.zones = zones
    f.common_zone_switch = common
    f.auto_setpoint = True
    f.auto_fan_speed = False
    f.force_auto_fan = False
    f.setpoint_max_bias = 2.0
    f.fan_full_deviation = 4.0
    f._conf = conf or {}
    f._started = True
    f._pass_lock = asyncio.Lock()
    f._zones_by_id = {z["id"]: z for z in zones}
    f._zones_by_entity = {}
    for z in zones:
        watched = {z["local_sensor"]} | {c["entity"] for c in z.get("conditions", [])}
        for e in watched:
            f._zones_by_entity.setdefault(e, []).append(z)
    f._setpoint_base = base
    f._last_commanded_setpoint = None
    f._last_restored_base = None
    f._fan_base = None
    f._last_commanded_fan = None
    f._last_restored_fan = None
    f._restore_verify_unsub = None
    f._restore_verify_attempts = 0
    f._cond_effective, f._cond_pending, f._cond_timers = {}, {}, {}
    f._targets = dict(targets or {})
    f._enabled = {z["id"]: True for z in zones}
    f._offset_values = {}
    f._status_listeners = {}
    f._auto_fan_speed_override = None
    f._auto_setpoint_override = None
    f._notify_hub = lambda: None
    f._mode = M._mode
    f._fan_rank = M._fan_rank
    f._to_float = M._to_float
    for name in METHODS:
        setattr(f, name, getattr(M, name).__get__(f))
    return f


def climate(mode, unit_temp, setpoint, **extra):
    attrs = {"current_temperature": unit_temp, "temperature": setpoint,
             "target_temp_step": 0.5, "min_temp": 16.0, "max_temp": 32.0}
    attrs.update(extra)
    return State(mode, attrs)


def run(coro):
    return asyncio.run(coro)


ZONE = {"id": "z1", "name": "Zone 1", "zone_switch": "switch.z1",
        "local_sensor": "sensor.z1"}
DOOR_ZONE = {**ZONE, "conditions": [{"entity": "binary_sensor.door", "state": "off"}]}


def trigger(old, new):
    return types.SimpleNamespace(data={"old_state": State(old), "new_state": State(new)})


# ------------------------------------------------------------ tests


def test_numbers():
    print("number parsing")
    check("plain number", C._num("21.5") == 21.5)
    check("nan rejected", C._num("nan") is None)
    check("inf rejected", C._num("inf") is None)
    check("text rejected", C._num("warm") is None)
    check("None rejected", C._num(None) is None)
    check("unavailable state reads as None", M._to_float(State("unavailable")) is None)


def test_setpoint_bound():
    print("setpoint: the user's setpoint is a hard limit")
    cases = [
        ("cool, hot return air: never above base", "cool", 27.0, 24.0, "25.0", 24.0, "==24"),
        ("cool: pushes below base when needed", "cool", 24.0, 24.0, "25.0", 24.0, "<24"),
        ("cool at rest: capped at base", "cool", 25.6, 24.0, "21.9", 24.0, "==24"),
        ("heat, cold return air: never below base", "heat", 18.0, 21.0, "19.0", 21.0, "==21"),
        ("heat: pushes above base when needed", "heat", 22.0, 21.0, "19.0", 21.0, ">21"),
        ("heat at rest: floored at base", "heat", 21.4, 21.0, "22.1", 21.0, "==21"),
    ]
    for label, mode, unit, shown, room, base, want in cases:
        m = manager({"climate.ac": climate(mode, unit, shown),
                     "switch.z1": State("on"), "sensor.z1": State(room)},
                    [ZONE], {"z1": 22.0}, base=base)
        got = m._setpoint_eval().get("desired_setpoint")
        ok = {"==24": got == 24.0, "<24": got is not None and got < 24.0,
              "==21": got == 21.0, ">21": got is not None and got > 21.0}[want]
        check(label, ok, f"got {got}")
    m = manager({"climate.ac": climate("cool", "nan", 24.0)}, [ZONE], {"z1": 22.0})
    check("a nan unit reading is 'Unavailable', not a crash",
          m._setpoint_eval()["status"] == "Unavailable")


def test_base_memory():
    print("setpoint: remembering and restoring the user's value")
    m = manager({"climate.ac": climate("cool", 27.0, 22.5)})
    m._last_commanded_setpoint, m._last_restored_base = 22.5, 24.0
    m.capture_setpoint_base()
    check("ignored off-restore detected: keeps 24", m._setpoint_base == 24.0)

    m = manager({"climate.ac": climate("cool", 27.0, 22.0)})
    m._last_commanded_setpoint, m._last_restored_base = 22.5, 24.0
    m.capture_setpoint_base()
    check("user change while off is captured", m._setpoint_base == 22.0)

    m = manager({"climate.ac": climate("off", None, 20.0)}, base=24.0)
    run(m._async_apply_setpoint())
    run(m._async_apply_setpoint())
    check("restores once when the unit turns off, then stays quiet",
          m.hass.services.calls == [("climate", "set_temperature",
                                     {"entity_id": "climate.ac", "temperature": 24.0})])

    m = manager({"climate.ac": State("unavailable")}, base=24.0)
    run(m.async_restore_setpoint_base())
    check("unreachable unit: base kept for later, nothing sent",
          m._setpoint_base == 24.0 and not m.hass.services.calls)

    m = manager({"climate.ac": State("unavailable")})
    m._fan_base = "3"
    run(m.async_restore_fan_base())
    check("unreachable unit: fan base kept too", m._fan_base == "3")


def test_stop_switches():
    print("turning the auto features off")
    m = manager({"climate.ac": State("cool", {"fan_mode": "5"})})
    m._fan_base = "2"
    run(m.async_stop_auto_fan())
    check("auto fan off puts the user's fan mode back",
          ("climate", "set_fan_mode", {"entity_id": "climate.ac", "fan_mode": "2"})
          in m.hass.services.calls)

    m = manager({"climate.ac": climate("cool", 24.0, 22.0)}, base=24.0)
    run(m.async_stop_auto_setpoint())
    check("auto setpoint off puts the user's setpoint back",
          ("climate", "set_temperature", {"entity_id": "climate.ac", "temperature": 24.0})
          in m.hass.services.calls)


def test_restore_check():
    print("restore check after power-off")
    m = manager({"climate.ac": climate("off", None, 20.0)})
    m._last_restored_base, m._last_commanded_setpoint = 24.0, 20.0
    scheduled = []
    m._schedule_restore_verify = lambda first=False: scheduled.append(1)
    run(m._async_check_restore())
    check("re-writes a restore the unit ignored",
          ("climate", "set_temperature", {"entity_id": "climate.ac", "temperature": 24.0})
          in m.hass.services.calls and scheduled)

    m = manager({"climate.ac": climate("off", None, 22.0)})
    m._last_restored_base, m._last_commanded_setpoint = 24.0, 20.0
    run(m._async_check_restore())
    check("leaves a user's own change alone", not m.hass.services.calls)

    m = manager({"climate.ac": climate("cool", 24.0, 20.0)})
    m._last_restored_base = 24.0
    run(m._async_check_restore())
    check("does nothing once the unit is back on", not m.hass.services.calls)


def test_retries():
    print("climate write retries")
    C.SWITCH_RETRY_DELAY = 0
    m = manager({"climate.ac": climate("cool", 24.0, 24.0),
                 "switch.z1": State("on"), "sensor.z1": State("25.0")},
                [ZONE], {"z1": 22.0}, base=24.0)
    m.hass.services = Services(fail_first=2)
    run(m._async_apply_setpoint())
    check("third attempt sticks", len(m.hass.services.calls) == 3
          and m._last_commanded_setpoint is not None)
    m.hass.services = Services(fail_first=99)
    m._last_commanded_setpoint = None
    run(m._async_apply_setpoint())
    check("gives up cleanly at the cap", m._last_commanded_setpoint is None)


def test_zone_decisions():
    print("zone decisions")

    def decide(room, mode="cool", turn_on=False, common=None, sensor_state=None):
        m = manager({"climate.ac": climate(mode, 24.0, 24.0),
                     "switch.z1": State("off"),
                     "sensor.z1": State(sensor_state or room)},
                    [ZONE], {"z1": 22.0}, common=common)
        return m._decide_zone(ZONE, turn_on=turn_on)

    check("too warm -> open", decide("25.0") == (True, decide("25.0")[1]))
    check("in band -> hold", decide("22.1") is None)
    check("in band at turn-on -> open", (decide("22.1", turn_on=True) or [None])[0] is True)
    check("cool enough -> close", (decide("21.5") or [None])[0] is False)
    check("unreadable sensor -> fail-safe open",
          (decide("x", sensor_state="unavailable") or [None])[0] is True)
    check("unit unavailable -> hold, even with an unreadable sensor",
          decide("x", mode="unavailable", sensor_state="unavailable") is None)
    check("unit off -> close", (decide("25.0", mode="off") or [None])[0] is False)

    m = manager({"climate.ac": climate("cool", 24.0, 24.0),
                 "switch.z1": State("off"), "sensor.z1": State("22.1")},
                [ZONE], {"z1": 22.0}, common="switch.common")
    check("common zone opens when nothing else is open",
          (m._decide_common({}) or [None])[0] is True)


def test_offsets_and_targets():
    print("offsets and targets")
    m = manager({}, [ZONE])
    m.set_zone_offset("z1", "cool_upper", -2)
    check("negative offset clamped to 0", m.get_zone_offset(ZONE, "cool_upper") == 0.0)
    m.set_zone_offset("z1", "cool_lower", "junk")
    check("non-number offset ignored (falls back to default)",
          m.get_zone_offset(ZONE, "cool_lower") == C.DEFAULT_OFFSET)
    m.set_zone_target("z1", "nan")
    check("non-number target ignored", m.get_zone_target("z1") is None)


def test_condition_debounce():
    print("condition debounce")
    timers = []
    real = C.async_call_later

    def call_later(hass, delay, action):
        entry = {"cancelled": False}
        timers.append(entry)
        return lambda: entry.update(cancelled=True)

    C.async_call_later = call_later
    states = {"climate.ac": climate("cool", 24.0, 24.0), "switch.z1": State("on"),
              "sensor.z1": State("25.0"), "binary_sensor.door": State("off")}
    m = manager(states, [DOOR_ZONE], {"z1": 22.0})
    m._decide_zone(DOOR_ZONE)
    states["binary_sensor.door"] = State("on")
    check("door opened briefly: zone stays open", m._decide_zone(DOOR_ZONE)[0] is True)
    states["binary_sensor.door"] = State("off")
    check("door shut again: still open, timer cancelled",
          m._decide_zone(DOOR_ZONE)[0] is True and timers[0]["cancelled"])
    states["binary_sensor.door"] = State("on")
    m._decide_zone(DOOR_ZONE)
    m._cond_pending["z1"] = (False, time.monotonic() - C.CONDITION_DEBOUNCE_SECONDS - 1)
    check("door open past the wait: zone closes", m._decide_zone(DOOR_ZONE)[0] is False)
    C.async_call_later = real


def test_trigger():
    print("auto power-on")
    conf = {"trigger_high": 31.0, "trigger_low": 17.0}

    def fire(old, new, mode="off"):
        m = manager({"climate.ac": climate(mode, None, 24.0)}, conf=conf)
        run(m._handle_trigger_change(trigger(old, new)))
        return m.hass.services.calls

    check("crossing above -> on and cool",
          [c[1] for c in fire("30", "32")] == ["turn_on", "set_hvac_mode"]
          and fire("30", "32")[1][2]["hvac_mode"] == "cool")
    check("crossing below -> heat", fire("18", "16")[1][2]["hvac_mode"] == "heat")
    check("already above (no crossing) -> nothing", fire("32", "33") == [])
    check("turned off on a hot day stays off", fire("33", "34") == [])
    check("unit already running -> nothing", fire("30", "32", mode="cool") == [])
    check("sensor coming back from unavailable -> nothing", fire("unavailable", "33") == [])


def test_event_routing_and_lock():
    print("event routing and pass locking")
    states = {"climate.ac": climate("cool", 24.0, 24.0),
              "switch.z1": State("off"), "sensor.z1": State("25.0")}
    m = manager(states, [ZONE], {"z1": 22.0})
    seen = []
    real = m._async_apply_zone_decisions

    async def spy(zones, turn_on=False):
        seen.append([z["id"] for z in zones])
        await real(zones, turn_on=turn_on)

    m._async_apply_zone_decisions = spy
    run(m._handle_zone_entity_change(types.SimpleNamespace(
        data={"entity_id": "sensor.unrelated", "new_state": State("1")})))
    check("unrelated entity -> no pass", seen == [])
    run(m._handle_zone_entity_change(types.SimpleNamespace(
        data={"entity_id": "sensor.z1", "new_state": State("25.0")})))
    check("zone's sensor -> that zone is evaluated", seen == [["z1"]])

    m = manager(states, [ZONE], {"z1": 22.0})
    m.hass.services = Services(delay=0.01)
    active = {"now": 0, "max": 0}
    real = m._async_apply_zone_decisions

    async def counted(zones, turn_on=False):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await real(zones, turn_on=turn_on)
        active["now"] -= 1

    m._async_apply_zone_decisions = counted

    async def two_at_once():
        await asyncio.gather(m.async_manage_all(), m.async_manage_all())

    run(two_at_once())
    check("two passes at once never overlap", active["max"] == 1, f"max={active['max']}")


def test_config_flow():
    print("config flow")
    zones = [{"id": "a", "zone_switch": "switch.x"}, {"id": "b", "zone_switch": "switch.y"}]
    check("switch used by another zone is rejected", CF._switch_in_use("switch.x", zones))
    check("a zone keeping its own switch is fine",
          not CF._switch_in_use("switch.x", zones, exclude_id="a"))
    check("an unused switch is fine", not CF._switch_in_use("switch.z", zones))

    flow = CF.SmarterZonesConfigFlow()
    flow._zones = [{"id": "a", "zone_switch": "switch.x"}]
    new_zone = {"name": "B", "zone_switch": "switch.x", "local_sensor": "sensor.b",
                "target_init": 22.0, "cool_upper": 0.3, "cool_lower": 0.3,
                "heat_upper": 0.3, "heat_lower": 0.3, "add_conditions": False}
    result = run(flow.async_step_zone(dict(new_zone)))
    check("setup form shows the duplicate-switch error",
          result.get("errors") == {"zone_switch": "switch_in_use"})
    result = run(flow.async_step_zone({**new_zone, "zone_switch": "switch.new"}))
    check("a new switch goes on to the zones menu", result.get("type") == "menu")
    check("settings keys still cover every settings field",
          "trigger_low" in CF._settings_keys() and "common_zone_switch" in CF._settings_keys())


for test in [test_numbers, test_setpoint_bound, test_base_memory, test_stop_switches,
             test_restore_check, test_retries, test_zone_decisions,
             test_offsets_and_targets, test_condition_debounce, test_trigger,
             test_event_routing_and_lock, test_config_flow]:
    test()

print(f"\n{failures} FAILURE(S)" if failures else "\nall tests passed")
sys.exit(1 if failures else 0)
