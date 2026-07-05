"""Core zone-control logic for SmarterZones.

Listens for state changes on the climate device, each zone's local temperature
sensor and condition entities, then turns each zone's switch on or off. The
target temperature and the smart-control enable flag are owned by this
integration's own number/switch entities, which push their values here.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from enum import Enum

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)

from .const import (
    CONF_AUTO_FAN_SPEED,
    CONF_AUTO_POWER_ON,
    CONF_AUTO_SETPOINT,
    CONF_CLIMATE_DEVICE,
    CONF_COMMON_ZONE_SWITCH,
    CONF_CONDITION_ENTITY,
    CONF_CONDITION_STATE,
    CONF_CONDITIONS,
    CONF_COOL_LOWER,
    CONF_COOL_UPPER,
    CONF_FAN_AUTO_SUFFIX,
    CONF_FAN_SPEED_MODES,
    CONF_FORCE_AUTO_FAN,
    CONF_HEAT_LOWER,
    CONF_HEAT_UPPER,
    CONF_LOCAL_SENSOR,
    CONF_TRIGGER_HIGH,
    CONF_TRIGGER_LOW,
    CONF_TRIGGER_SENSOR,
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    CONF_ZONE_SWITCH,
    CONF_ZONES,
    CONDITION_DEBOUNCE_SECONDS,
    DEFAULT_FAN_AUTO_SUFFIX,
    DEFAULT_OFFSET,
    DEFAULT_TRIGGER_HIGH,
    DEFAULT_TRIGGER_LOW,
    FAN_FULL_DEVIATION,
    FAN_MAX_ZONE_BUMP,
    FAN_PER_ZONE_BUMP,
    RESTORE_VERIFY_ATTEMPTS,
    RESTORE_VERIFY_DELAY,
    SETPOINT_DEADBAND,
    SETPOINT_FULL_DEVIATION,
    SETPOINT_MAX_BIAS,
    SETPOINT_MAX_ZONE_BUMP,
    SETPOINT_MIN_BIAS,
    SETPOINT_PER_ZONE_BUMP,
    SETPOINT_REST_MARGIN,
    SWITCH_RETRY_ATTEMPTS,
    SWITCH_RETRY_DELAY,
    TARGET_TEMP_STEP,
    UNAVAILABLE_STATES,
)

_LOGGER = logging.getLogger(__name__)


class ACMode(Enum):
    COOLING = 1
    HEATING = 2
    OFF = 3
    OTHER = 4
    HEAT_COOL = 5  # Daikin "auto" band mode (heat_cool)


class SmarterZonesManager:
    """Owns all listeners and the decision logic for one climate device."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._unsubs: list = []
        # Pushed in by the per-zone entities:
        self._enabled: dict[str, bool] = {}   # smart-control switch
        self._targets: dict[str, float] = {}  # target-temperature number
        self._offset_values: dict[str, dict[str, float]] = {}  # offset numbers
        # Listeners that want to know when a zone's open/closed decision changes:
        self._status_listeners: dict[str, list] = {}
        # Hub-level listeners (e.g. the fan-decision sensor), refreshed whenever
        # the fan logic runs or the auto-fan toggle changes:
        self._hub_listeners: list = []
        # Runtime override for the auto-fan-speed toggle (None = follow config):
        self._auto_fan_speed_override: bool | None = None
        # Runtime full-speed deviation (degrees), driven by the hub number entity:
        self._fan_full_deviation: float = FAN_FULL_DEVIATION
        # Runtime override for the auto-setpoint toggle (None = follow config):
        self._auto_setpoint_override: bool | None = None
        # Runtime max setpoint bias (degrees), driven by the hub number entity:
        self._setpoint_max_bias: float = SETPOINT_MAX_BIAS
        # The unit's target temperature as it was when auto-setpoint was switched
        # on (the user's "base" level), re-instated when it's switched off:
        self._setpoint_base: float | None = None
        # The last biased setpoint we commanded, and the last base we tried to
        # restore. Together they let the turn-on capture recognise a display that
        # is still *our own* bias (an off-state restore some units ignore) and
        # fall back to the real base instead of corrupting it:
        self._last_commanded_setpoint: float | None = None
        self._last_restored_base: float | None = None
        # The fan-mode equivalents: the mode the unit had at power-on (restored
        # when it turns off), the last speed auto-fan commanded, and the last
        # mode we tried to restore:
        self._fan_base: str | None = None
        self._last_commanded_fan: str | None = None
        self._last_restored_fan: str | None = None
        # Pending delayed check that the off-state restores actually took:
        self._restore_verify_unsub = None
        self._restore_verify_attempts = 0
        # Condition debounce, per zone id: the condition state the control
        # logic is acting on, a pending flip (target state, monotonic start
        # time), and the one-shot timers that re-evaluate when a pending flip
        # has held long enough. Keeps a flapping door sensor from rapidly
        # cycling a damper; displays still show the raw state.
        self._cond_effective: dict[str, bool] = {}
        self._cond_pending: dict[str, tuple[bool, float]] = {}
        self._cond_timers: dict[str, object] = {}

    # ----------------------------------------------------------- config views

    @property
    def _conf(self) -> dict:
        return {**self.entry.data, **self.entry.options}

    @property
    def climate_device(self) -> str:
        return self._conf[CONF_CLIMATE_DEVICE]

    @property
    def common_zone_switch(self) -> str | None:
        return self._conf.get(CONF_COMMON_ZONE_SWITCH)

    @property
    def zones(self) -> list[dict]:
        return self._conf.get(CONF_ZONES, [])

    @property
    def force_auto_fan(self) -> bool:
        return bool(self._conf.get(CONF_FORCE_AUTO_FAN, False))

    @property
    def fan_auto_suffix(self) -> str:
        return self._conf.get(CONF_FAN_AUTO_SUFFIX, DEFAULT_FAN_AUTO_SUFFIX)

    @property
    def fan_speed_modes_defined(self) -> bool:
        """True if the user has configured an explicit ordered fan-speed list."""
        value = self._conf.get(CONF_FAN_SPEED_MODES)
        return bool(value and str(value).strip())

    @property
    def fan_full_deviation(self) -> float:
        """Degrees the worst open zone is from target that maps to full speed.

        Driven live by the hub 'Full-speed deviation' number entity.
        """
        return self._fan_full_deviation

    @property
    def auto_fan_speed(self) -> bool:
        if self._auto_fan_speed_override is not None:
            return self._auto_fan_speed_override
        return bool(self._conf.get(CONF_AUTO_FAN_SPEED, False))

    @property
    def auto_setpoint(self) -> bool:
        if self._auto_setpoint_override is not None:
            return self._auto_setpoint_override
        return bool(self._conf.get(CONF_AUTO_SETPOINT, False))

    @property
    def setpoint_max_bias(self) -> float:
        """Max degrees the setpoint is pushed past the unit's reading at full demand.

        Driven live by the hub 'Setpoint bias' number entity.
        """
        return self._setpoint_max_bias

    def set_auto_fan_speed(self, enabled: bool) -> None:
        """Runtime toggle for auto fan speed (from the hub switch)."""
        previous = self.auto_fan_speed
        self._auto_fan_speed_override = enabled
        if enabled != previous:
            _LOGGER.info("Auto fan speed %s", "enabled" if enabled else "disabled")
        else:
            _LOGGER.debug(
                "Auto fan speed set to %s (unchanged)", "on" if enabled else "off"
            )
        self._notify_hub()

    def set_fan_full_deviation(self, value: float) -> None:
        """Runtime setter for the full-speed deviation (from the hub number)."""
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        previous = self._fan_full_deviation
        self._fan_full_deviation = value
        if value != previous:
            _LOGGER.info("Fan full-speed deviation set to %.1f°", value)
        self._notify_hub()

    async def async_refresh_fan(self) -> None:
        """Re-evaluate just the fan speed (e.g. after a tuning change)."""
        await self._async_apply_fan()

    def set_auto_setpoint(self, enabled: bool) -> None:
        """Runtime toggle for auto target temperature (from the hub switch)."""
        previous = self.auto_setpoint
        self._auto_setpoint_override = enabled
        if enabled != previous:
            _LOGGER.info(
                "Auto target temperature %s", "enabled" if enabled else "disabled"
            )
        else:
            _LOGGER.debug(
                "Auto target temperature set to %s (unchanged)",
                "on" if enabled else "off",
            )
        self._notify_hub()

    def set_setpoint_max_bias(self, value: float) -> None:
        """Runtime setter for the max setpoint bias (from the hub number)."""
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        previous = self._setpoint_max_bias
        self._setpoint_max_bias = value
        if value != previous:
            _LOGGER.info("Setpoint bias set to %.1f°", value)
        self._notify_hub()

    async def async_refresh_setpoint(self) -> None:
        """Re-evaluate just the auto setpoint (e.g. after a tuning change)."""
        await self._async_apply_setpoint()

    # ----------------------------------------------- base setpoint memory

    @property
    def setpoint_base(self) -> float | None:
        """The unit setpoint remembered from when auto-setpoint was switched on."""
        return self._setpoint_base

    def set_setpoint_base(self, value: float | None) -> None:
        """Seed the remembered base (e.g. restored from the switch across restarts)."""
        try:
            self._setpoint_base = float(value) if value is not None else None
        except (TypeError, ValueError):
            self._setpoint_base = None

    def capture_setpoint_base(self) -> None:
        """Remember the unit's current target temperature as the user's base level.

        Called when auto target temperature is switched on, *before* any bias is
        applied, so the value stored is the user's own setpoint and can be
        re-instated when the feature is switched off.
        """
        state = self.hass.states.get(self.climate_device)
        value = state.attributes.get("temperature") if state else None
        try:
            value = float(value) if value is not None else None
        except (TypeError, ValueError):
            value = None
        # If the displayed target is still the last bias *we* wrote, the off-state
        # restore never took on the device (some units ignore writes while off).
        # Capturing it would corrupt the base a little more every on/off cycle, so
        # fall back to the base we last tried to restore.
        if (
            value is not None
            and self._last_commanded_setpoint is not None
            and self._last_restored_base is not None
            and abs(value - self._last_commanded_setpoint) < SETPOINT_DEADBAND
            and abs(value - self._last_restored_base) >= SETPOINT_DEADBAND
        ):
            _LOGGER.warning(
                "Auto target temperature: displayed target %.1f° is still our own "
                "bias (the off-state restore didn't take on the unit); keeping the "
                "remembered base %.1f° instead",
                value,
                self._last_restored_base,
            )
            value = self._last_restored_base
        self._setpoint_base = value
        self._last_commanded_setpoint = None
        if value is not None:
            _LOGGER.info(
                "Auto target temperature on: remembered base setpoint %.1f°", value
            )
        else:
            _LOGGER.debug("Auto target temperature on: no base setpoint to remember")

    async def async_restore_setpoint_base(self, clear: bool = True) -> None:
        """Re-instate the remembered base target temperature, then forget it.

        Called when auto-setpoint is switched off and the first time the unit is
        found off while auto-setpoint stays on. ``clear=True`` (the default) wipes
        the remembered base afterwards so we don't keep writing to the unit while it
        stays off - it's re-captured next time the unit is turned on. The base is
        written even when the unit is off (that's the point - the displayed target
        returns to the user's level rather than sitting at our bias); only an
        unreachable (unavailable/unknown) device is skipped.
        """
        value = self._setpoint_base
        if clear:
            self._setpoint_base = None
        if value is None:
            _LOGGER.debug("Auto target temperature: no base setpoint to restore")
            return
        state = self.hass.states.get(self.climate_device)
        state_l = state.state.lower() if state else None
        if state is None or state_l in ("unavailable", "unknown"):
            _LOGGER.debug(
                "Auto target temperature: unit %s unreachable; can't restore "
                "base setpoint %.1f°",
                self.climate_device,
                value,
            )
            return
        current = state.attributes.get("temperature")
        try:
            current = float(current) if current is not None else None
        except (TypeError, ValueError):
            current = None
        if current is not None and abs(current - value) < SETPOINT_DEADBAND:
            self._last_restored_base = value
            _LOGGER.debug(
                "Auto target temperature: setpoint already ~%.1f°; no restore needed",
                value,
            )
            return
        if await self._async_climate_call(
            "set_temperature",
            {"temperature": value},
            f"re-instate base setpoint {value:.1f}°",
        ):
            self._last_restored_base = value
            if state_l != "off":
                # Written while the unit is on, so it reliably took; no stale
                # bias remains for the turn-on capture to guard against.
                self._last_commanded_setpoint = None
            else:
                # Off-state writes are the unreliable ones - come back shortly
                # and confirm the device actually took it.
                self._schedule_restore_verify(first=True)
            _LOGGER.info(
                "Auto target temperature: re-instated base setpoint %.1f°", value
            )

    # ------------------------------------------------- base fan-mode memory

    @property
    def fan_base(self) -> str | None:
        """The fan mode remembered from when the unit was powered on."""
        return self._fan_base

    def set_fan_base(self, value: str | None) -> None:
        """Seed the remembered fan mode (restored from the switch across restarts)."""
        self._fan_base = value or None

    def capture_fan_base(self) -> None:
        """Remember the unit's current fan mode as the user's base level.

        Mirrors ``capture_setpoint_base``: called on power-on before auto fan
        speed makes any change, with the same guard against capturing a speed
        *we* set (an off-state restore the unit ignored).
        """
        state = self.hass.states.get(self.climate_device)
        value = state.attributes.get("fan_mode") if state else None
        value = value or None
        if (
            value is not None
            and self._last_commanded_fan is not None
            and self._last_restored_fan is not None
            and value == self._last_commanded_fan
            and value != self._last_restored_fan
        ):
            _LOGGER.warning(
                "Auto fan speed: displayed fan mode '%s' is still our own choice "
                "(the off-state restore didn't take on the unit); keeping the "
                "remembered mode '%s' instead",
                value,
                self._last_restored_fan,
            )
            value = self._last_restored_fan
        self._fan_base = value
        self._last_commanded_fan = None
        if value:
            _LOGGER.info("Auto fan speed: remembered base fan mode '%s'", value)
        else:
            _LOGGER.debug("Auto fan speed: no base fan mode to remember")

    async def async_restore_fan_base(self, clear: bool = True) -> None:
        """Re-instate the remembered fan mode, then forget it.

        Called the first time the unit is found off. Skips only an unreachable
        device; the write is attempted while off on purpose, with the delayed
        verification catching units that ignore it.
        """
        value = self._fan_base
        if clear:
            self._fan_base = None
        if not value:
            _LOGGER.debug("Auto fan speed: no base fan mode to restore")
            return
        state = self.hass.states.get(self.climate_device)
        state_l = state.state.lower() if state else None
        if state is None or state_l in ("unavailable", "unknown"):
            _LOGGER.debug(
                "Auto fan speed: unit %s unreachable; can't restore fan mode '%s'",
                self.climate_device,
                value,
            )
            return
        self._last_restored_fan = value
        current_fan = state.attributes.get("fan_mode")
        if current_fan == value:
            _LOGGER.debug(
                "Auto fan speed: fan mode already '%s'; no restore needed", value
            )
            return
        if await self._async_climate_call(
            "set_fan_mode",
            {"fan_mode": value},
            f"re-instate base fan mode '{value}'",
        ):
            if state_l != "off":
                self._last_commanded_fan = None
            else:
                self._schedule_restore_verify(first=True)
            _LOGGER.info("Auto fan speed: re-instated base fan mode '%s'", value)

    # -------------------------------------------- off-restore verification

    def _cancel_restore_verify(self) -> None:
        if self._restore_verify_unsub is not None:
            self._restore_verify_unsub()
            self._restore_verify_unsub = None

    def _schedule_restore_verify(self, first: bool = False) -> None:
        """Check RESTORE_VERIFY_DELAY seconds later that the restores took."""
        if first:
            self._restore_verify_attempts = 0
        self._cancel_restore_verify()
        self._restore_verify_unsub = async_call_later(
            self.hass, RESTORE_VERIFY_DELAY, self._async_verify_restore
        )

    async def _async_verify_restore(self, _now) -> None:
        """Confirm the off-state setpoint/fan restores match expectations.

        Runs RESTORE_VERIFY_DELAY seconds after an off-restore. If the device
        still shows *our own* stale value it re-writes and re-checks (up to
        RESTORE_VERIFY_ATTEMPTS retries). A value that matches neither the
        expected base nor our last command means the user changed it while
        off, so it's left alone. Only acts while the unit is still off - once
        it's back on, the normal logic owns the device again.
        """
        self._restore_verify_unsub = None
        state = self.hass.states.get(self.climate_device)
        if state is None or state.state.lower() != "off":
            _LOGGER.debug("Restore check: unit no longer off; nothing to verify")
            return
        retried = False

        expected = self._last_restored_base
        if expected is not None:
            current = state.attributes.get("temperature")
            try:
                current = float(current) if current is not None else None
            except (TypeError, ValueError):
                current = None
            mismatch = current is None or abs(current - expected) >= SETPOINT_DEADBAND
            ours = (
                self._last_commanded_setpoint is None
                or (
                    current is not None
                    and abs(current - self._last_commanded_setpoint)
                    < SETPOINT_DEADBAND
                )
            )
            if mismatch and ours:
                _LOGGER.warning(
                    "Restore check: setpoint reads %s, expected %.1f°; re-writing",
                    current,
                    expected,
                )
                await self._async_climate_call(
                    "set_temperature",
                    {"temperature": expected},
                    f"re-write base setpoint {expected:.1f}°",
                )
                # Re-check regardless of the outcome; the follow-up pass is what
                # confirms the value actually stuck.
                retried = True
            elif mismatch:
                _LOGGER.debug(
                    "Restore check: setpoint %s isn't ours; leaving it alone", current
                )

        expected_fan = self._last_restored_fan
        if expected_fan:
            current_fan = state.attributes.get("fan_mode")
            fan_ours = (
                self._last_commanded_fan is None
                or current_fan == self._last_commanded_fan
            )
            if current_fan != expected_fan and fan_ours:
                _LOGGER.warning(
                    "Restore check: fan mode is '%s', expected '%s'; re-writing",
                    current_fan,
                    expected_fan,
                )
                await self._async_climate_call(
                    "set_fan_mode",
                    {"fan_mode": expected_fan},
                    f"re-write base fan mode '{expected_fan}'",
                )
                retried = True
            elif current_fan != expected_fan:
                _LOGGER.debug(
                    "Restore check: fan mode '%s' isn't ours; leaving it alone",
                    current_fan,
                )

        if not retried:
            _LOGGER.debug("Restore check: setpoint and fan match expectations")
            return
        if self._restore_verify_attempts < RESTORE_VERIFY_ATTEMPTS:
            self._restore_verify_attempts += 1
            self._schedule_restore_verify()
        else:
            _LOGGER.warning(
                "Restore check: still not matching after %d attempts; giving up "
                "(the unit may ignore writes while off - values will be corrected "
                "at next power-on)",
                RESTORE_VERIFY_ATTEMPTS + 1,
            )

    # ------------------------------------------------------------- lifecycle

    async def async_setup(self) -> None:
        """Register all state listeners and do a first evaluation."""
        _LOGGER.info(
            "Setting up SmarterZones for %s: %d zone(s); common_zone=%s, "
            "force_auto_fan=%s, auto_fan_speed=%s, auto_setpoint=%s, auto_power_on=%s",
            self.climate_device,
            len(self.zones),
            self.common_zone_switch or "none",
            self.force_auto_fan,
            self.auto_fan_speed,
            self.auto_setpoint,
            bool(self._conf.get(CONF_AUTO_POWER_ON)),
        )
        for zone in self.zones:
            _LOGGER.debug(
                "Zone '%s': switch=%s sensor=%s conditions=%d",
                zone[CONF_ZONE_NAME],
                zone[CONF_ZONE_SWITCH],
                zone[CONF_LOCAL_SENSOR],
                len(zone.get(CONF_CONDITIONS, [])),
            )
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.climate_device], self._handle_climate_change
            )
        )

        zone_entities: set[str] = set()
        condition_count = 0
        for zone in self.zones:
            zone_entities.add(zone[CONF_LOCAL_SENSOR])
            for cond in zone.get(CONF_CONDITIONS, []):
                zone_entities.add(cond[CONF_CONDITION_ENTITY])
                condition_count += 1
        if zone_entities:
            _LOGGER.debug(
                "Watching %d entit(ies) for zone sensors and conditions "
                "(%d condition(s)): %s",
                len(zone_entities),
                condition_count,
                ", ".join(sorted(zone_entities)),
            )
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, list(zone_entities), self._handle_zone_entity_change
                )
            )

        trigger = self._conf.get(CONF_TRIGGER_SENSOR)
        if self._conf.get(CONF_AUTO_POWER_ON) and trigger:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [trigger], self._handle_trigger_change
                )
            )

        # If the unit is already running when we set up (e.g. after a HA
        # restart), treat it like a turn-on so in-band zones open.
        device_state = self._device_state()
        active = device_state is not None and device_state.lower() not in (
            "off",
            "unavailable",
            "unknown",
        )
        await self.async_manage_all(turn_on=active)

    @callback
    def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_restore_verify()
        self._reset_condition_tracking()

    # ------------------------------------------- entity coordination

    @callback
    def set_zone_enabled(self, zone_id: str, enabled: bool) -> None:
        self._enabled[zone_id] = enabled

    def is_zone_enabled(self, zone_id: str) -> bool:
        return self._enabled.get(zone_id, True)

    @callback
    def set_zone_target(self, zone_id: str, target: float) -> None:
        self._targets[zone_id] = target

    def get_zone_target(self, zone_id: str) -> float | None:
        return self._targets.get(zone_id)

    def get_zone(self, zone_id: str) -> dict | None:
        """Return the live config for a zone, or None if it no longer exists."""
        return next(
            (z for z in self.zones if z[CONF_ZONE_ID] == zone_id), None
        )

    @callback
    def set_zone_offset(self, zone_id: str, key: str, value: float) -> None:
        self._offset_values.setdefault(zone_id, {})[key] = float(value)

    def _is_ready(self, zone_id: str) -> bool:
        """A zone is only evaluated once its number and switch have registered.

        This avoids acting on a half-initialised zone during startup, when
        platform setup order isn't guaranteed.
        """
        return zone_id in self._targets and zone_id in self._enabled

    @callback
    def register_status_listener(self, zone_id: str, update_cb) -> None:
        self._status_listeners.setdefault(zone_id, []).append(update_cb)

    @callback
    def unregister_status_listener(self, zone_id: str, update_cb) -> None:
        if zone_id in self._status_listeners:
            self._status_listeners[zone_id].remove(update_cb)

    @callback
    def _notify_status(self, zone_id: str) -> None:
        for cb in self._status_listeners.get(zone_id, []):
            cb()

    @callback
    def _notify_all_status(self) -> None:
        """Refresh every zone's status sensors (used on climate device changes)."""
        for zone in self.zones:
            self._notify_status(zone[CONF_ZONE_ID])

    @callback
    def register_hub_listener(self, update_cb) -> None:
        self._hub_listeners.append(update_cb)

    @callback
    def unregister_hub_listener(self, update_cb) -> None:
        if update_cb in self._hub_listeners:
            self._hub_listeners.remove(update_cb)

    @callback
    def _notify_hub(self) -> None:
        for cb in list(self._hub_listeners):
            cb()

    # --------------------------------------------------------------- helpers

    def _read_float(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state.lower() in UNAVAILABLE_STATES:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _device_state(self) -> str | None:
        state = self.hass.states.get(self.climate_device)
        return state.state if state else None

    @staticmethod
    def _mode(device_state: str) -> ACMode:
        mode = device_state.lower()
        if mode in ("heat_cool", "auto"):
            return ACMode.HEAT_COOL
        if "cool" in mode:
            return ACMode.COOLING
        if "heat" in mode:
            return ACMode.HEATING
        if mode == "off":
            return ACMode.OFF
        return ACMode.OTHER

    def get_zone_offset(self, zone: dict, key: str) -> float:
        """Live offset value, falling back to the setup value, then default."""
        live = self._offset_values.get(zone[CONF_ZONE_ID], {})
        if key in live:
            return float(live[key])
        return float(zone.get(key, DEFAULT_OFFSET))

    def _offsets(self, zone: dict, device_state: str) -> tuple[float, float]:
        """Return (upper, lower) offset for the active mode.

        Live values (from the per-zone offset number entities) take priority,
        then the value stored at setup, then the default.
        """
        state = device_state.lower()
        if state in ("heat_cool", "auto"):
            return (
                self.get_zone_offset(zone, CONF_COOL_UPPER),
                self.get_zone_offset(zone, CONF_HEAT_LOWER),
            )
        if "heat" in state:
            return (
                self.get_zone_offset(zone, CONF_HEAT_UPPER),
                self.get_zone_offset(zone, CONF_HEAT_LOWER),
            )
        return (
            self.get_zone_offset(zone, CONF_COOL_UPPER),
            self.get_zone_offset(zone, CONF_COOL_LOWER),
        )

    def comfort_range(self, zone: dict) -> dict | None:
        """Desired temperature ranges derived from target and offsets.

        Returns cooling/heating ranges plus the range currently in effect
        (based on the climate device's mode; a heat_cool band is assumed while
        the unit is off). None until a target is known.
        """
        target = self.get_zone_target(zone[CONF_ZONE_ID])
        if target is None:
            return None
        cool_up = self.get_zone_offset(zone, CONF_COOL_UPPER)
        cool_low = self.get_zone_offset(zone, CONF_COOL_LOWER)
        heat_up = self.get_zone_offset(zone, CONF_HEAT_UPPER)
        heat_low = self.get_zone_offset(zone, CONF_HEAT_LOWER)

        device_state = self._device_state()
        if device_state and device_state.lower() not in (
            "off",
            "unknown",
            "unavailable",
            "",
        ):
            assume = device_state
        else:
            assume = "heat_cool"
        upper, lower = self._offsets(zone, assume)

        return {
            "target": target,
            "cooling": (round(target - cool_low, 2), round(target + cool_up, 2)),
            "heating": (round(target - heat_low, 2), round(target + heat_up, 2)),
            "active": (round(target - lower, 2), round(target + upper, 2)),
            "active_mode": assume.lower(),
        }

    def _conditions_met(self, zone: dict) -> bool:
        for cond in zone.get(CONF_CONDITIONS, []):
            entity = cond[CONF_CONDITION_ENTITY]
            state = self.hass.states.get(entity)
            actual = state.state.lower() if state else "unknown"
            required = str(cond[CONF_CONDITION_STATE]).lower()
            if actual != required:
                _LOGGER.debug(
                    "%s: condition failed - %s is '%s', needs '%s'",
                    zone[CONF_ZONE_NAME],
                    entity,
                    actual,
                    required,
                )
                return False
        return True

    def conditions_met(self, zone: dict) -> bool:
        """Public: are all of a zone's conditions currently satisfied?"""
        return self._conditions_met(zone)

    def _cancel_condition_timer(self, zone_id: str) -> None:
        cancel = self._cond_timers.pop(zone_id, None)
        if cancel is not None:
            cancel()

    @callback
    def _reset_condition_tracking(self) -> None:
        """Forget the debounce state so the next evaluation adopts raw truth.

        Used on unit turn-on (and unload): after a period where zones weren't
        being condition-managed, the control logic should honour an open
        window immediately rather than waiting out the debounce.
        """
        self._cond_effective.clear()
        self._cond_pending.clear()
        for zone_id in list(self._cond_timers):
            self._cancel_condition_timer(zone_id)

    def _effective_conditions_met(self, zone: dict) -> bool:
        """The debounced condition state the control logic acts on.

        A raw condition change (e.g. a door sensor flapping open/closed) only
        takes effect once it has held for CONDITION_DEBOUNCE_SECONDS, so brief
        flaps move no dampers at all. The first evaluation of a zone adopts the
        raw state immediately. Entering the pending window schedules a one-shot
        re-evaluation for when the debounce expires, since the sensor may go
        quiet and nothing else would re-trigger the decision.
        """
        if not zone.get(CONF_CONDITIONS):
            return True
        zone_id = zone[CONF_ZONE_ID]
        raw = self._conditions_met(zone)
        if zone_id not in self._cond_effective:
            self._cond_effective[zone_id] = raw
            return raw
        effective = self._cond_effective[zone_id]
        if raw == effective:
            # Steady (or a flap that returned in time): drop any pending flip.
            if self._cond_pending.pop(zone_id, None) is not None:
                self._cancel_condition_timer(zone_id)
                _LOGGER.debug(
                    "%s: condition flap settled back within %.0fs; no change",
                    zone[CONF_ZONE_NAME],
                    CONDITION_DEBOUNCE_SECONDS,
                )
            return effective
        now = time.monotonic()
        pending = self._cond_pending.get(zone_id)
        if pending is None or pending[0] != raw:
            self._cond_pending[zone_id] = (raw, now)
            self._cancel_condition_timer(zone_id)

            async def _recheck(_now, zid=zone_id):
                self._cond_timers.pop(zid, None)
                if (z := self.get_zone(zid)) is not None:
                    await self.async_manage_zone(z)

            self._cond_timers[zone_id] = async_call_later(
                self.hass, CONDITION_DEBOUNCE_SECONDS + 0.1, _recheck
            )
            _LOGGER.debug(
                "%s: conditions now %s; holding for %.0fs before acting",
                zone[CONF_ZONE_NAME],
                "met" if raw else "not met",
                CONDITION_DEBOUNCE_SECONDS,
            )
            return effective
        if now - pending[1] >= CONDITION_DEBOUNCE_SECONDS:
            self._cond_effective[zone_id] = raw
            self._cond_pending.pop(zone_id, None)
            self._cancel_condition_timer(zone_id)
            _LOGGER.debug(
                "%s: conditions %s held for %.0fs; acting on it",
                zone[CONF_ZONE_NAME],
                "met" if raw else "not met",
                CONDITION_DEBOUNCE_SECONDS,
            )
            return raw
        return effective

    def projected_status(self, zone: dict) -> str | None:
        """What the zone's open/closed state would be if the unit were running.

        Ignores whether the climate device is currently on. If the device is
        off/unknown, a heat_cool-style band is assumed (open if outside the band
        in either direction). Returns "Open", "Closed", or None when it can't be
        determined (no target or room temperature yet).
        """
        device_state = self._device_state()
        state_l = device_state.lower() if device_state else "off"

        # In dry/fan the manager simply opens the zone.
        if "dry" in state_l or "fan" in state_l:
            return "Open"

        if not self._conditions_met(zone):
            return "Closed"

        target = self.get_zone_target(zone[CONF_ZONE_ID])
        current = self._read_float(zone[CONF_LOCAL_SENSOR])
        if target is None or current is None:
            return None

        if state_l in ("off", "unknown", "unavailable", ""):
            mode = ACMode.HEAT_COOL
            assume_state = "heat_cool"
        else:
            mode = self._mode(device_state)
            assume_state = device_state

        upper, lower = self._offsets(zone, assume_state)
        max_temp = round(target + upper, 2)
        min_temp = round(target - lower, 2)

        if mode == ACMode.COOLING:
            # Open through the band; closed only once cooled past the lower edge.
            return "Open" if current > min_temp else "Closed"
        if mode == ACMode.HEATING:
            # Open through the band; closed only once warmed past the upper edge.
            return "Open" if current < max_temp else "Closed"
        # HEAT_COOL or any other active mode: open when outside the band.
        return "Open" if (current >= max_temp or current <= min_temp) else "Closed"

    async def _async_set_switch(
        self, entity_id: str, turn_on: bool, reason: str = ""
    ) -> None:
        current = self.hass.states.get(entity_id)
        desired = "on" if turn_on else "off"
        because = f" ({reason})" if reason else ""
        if current is not None and current.state == desired:
            _LOGGER.debug(
                "%s already %s; no change needed%s", entity_id, desired, because
            )
            return
        _LOGGER.info("Turning %s %s%s", entity_id, desired, because)
        service = "turn_on" if turn_on else "turn_off"
        # Calls are blocking (so they run one at a time and raise on failure)
        # and retried, because some zone controllers drop commands when several
        # arrive at once.
        last_err: Exception | None = None
        for attempt in range(1, SWITCH_RETRY_ATTEMPTS + 1):
            try:
                await self.hass.services.async_call(
                    "homeassistant",
                    service,
                    {"entity_id": entity_id},
                    blocking=True,
                )
                if attempt > 1:
                    _LOGGER.info(
                        "%s turned %s on attempt %d/%d",
                        entity_id,
                        desired,
                        attempt,
                        SWITCH_RETRY_ATTEMPTS,
                    )
                return
            except Exception as err:  # noqa: BLE001 - controller may reject command
                last_err = err
                _LOGGER.warning(
                    "Attempt %d/%d to turn %s %s failed: %s",
                    attempt,
                    SWITCH_RETRY_ATTEMPTS,
                    entity_id,
                    desired,
                    err,
                )
                if attempt < SWITCH_RETRY_ATTEMPTS:
                    await asyncio.sleep(SWITCH_RETRY_DELAY)
        _LOGGER.error(
            "Failed to turn %s %s after %d attempts; giving up: %s",
            entity_id,
            desired,
            SWITCH_RETRY_ATTEMPTS,
            last_err,
        )

    async def _async_climate_call(
        self, service: str, data: dict, description: str
    ) -> bool:
        """Call a climate service on the unit with retries, so writes stick.

        Same retry policy as the zone switches (the controller can drop
        commands that arrive close together): calls are blocking so failures
        raise, and each retry waits briefly before trying again. Returns True
        once a call succeeds, False when every attempt failed.
        """
        last_err: Exception | None = None
        for attempt in range(1, SWITCH_RETRY_ATTEMPTS + 1):
            try:
                await self.hass.services.async_call(
                    "climate",
                    service,
                    {"entity_id": self.climate_device, **data},
                    blocking=True,
                )
                if attempt > 1:
                    _LOGGER.info(
                        "%s succeeded on attempt %d/%d",
                        description,
                        attempt,
                        SWITCH_RETRY_ATTEMPTS,
                    )
                return True
            except Exception as err:  # noqa: BLE001 - unit may reject the command
                last_err = err
                _LOGGER.warning(
                    "Attempt %d/%d to %s failed: %s",
                    attempt,
                    SWITCH_RETRY_ATTEMPTS,
                    description,
                    err,
                )
                if attempt < SWITCH_RETRY_ATTEMPTS:
                    await asyncio.sleep(SWITCH_RETRY_DELAY)
        _LOGGER.error(
            "Failed to %s after %d attempts; giving up: %s",
            description,
            SWITCH_RETRY_ATTEMPTS,
            last_err,
        )
        return False

    async def async_set_zone_damper(self, zone_switch: str, turn_on: bool) -> None:
        """Manually open/close a zone's damper (from the per-zone Open switch).

        Uses the same retrying switch helper as the automatic logic so manual
        commands survive the controller dropping simultaneous requests.
        """
        await self._async_set_switch(zone_switch, turn_on, reason="manual override")

    # ------------------------------------------------------------- callbacks

    async def _handle_climate_change(self, event: Event[EventStateChangedData]) -> None:
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        old_s = old.state.lower() if old else "off"
        new_s = new.state.lower() if new else ""
        inactive = ("off", "unavailable", "unknown", "none", "")
        # Turn-on: was off/unavailable (or absent), now in a real mode.
        turned_on = old_s in inactive and new_s not in inactive
        if old_s != new_s:
            _LOGGER.info(
                "Climate device %s changed mode %s -> %s%s; re-evaluating all zones",
                self.climate_device,
                old_s or "?",
                new_s or "?",
                " (turn-on)" if turned_on else "",
            )
        else:
            _LOGGER.debug(
                "Climate device %s attributes changed (mode still %s); "
                "re-evaluating all zones",
                self.climate_device,
                new_s,
            )
        # (Re)capture the base only on a genuine power-on from *off*, before any
        # bias is applied. Deliberately NOT on recovery from unavailable/unknown:
        # mid-operation the displayed target is our bias, not the user's setpoint,
        # so re-capturing there would overwrite the base stored at start-up with a
        # biased value. (A plain setpoint change mid-operation is an attribute
        # change, not a turn-on, so it never re-captures either.)
        came_from_off = old_s in ("off", "none", "") and new_s not in inactive
        if came_from_off and self.auto_setpoint:
            self.capture_setpoint_base()
        if came_from_off and self.auto_fan_speed:
            self.capture_fan_base()
        if turned_on:
            # The unit is running again: the normal logic owns it, so any
            # pending off-restore verification is obsolete. Conditions are
            # honoured as they are right now (no debounce carry-over from
            # before the off period).
            self._cancel_restore_verify()
            self._reset_condition_tracking()
        await self.async_manage_all(turn_on=turned_on)
        # Guarantee every zone's status sensors reflect the new climate state,
        # even for zones the control logic skipped (deadband, manual, etc.).
        self._notify_all_status()

    async def _async_enforce_fan(self) -> None:
        state = self.hass.states.get(self.climate_device)
        if state is None or state.state == "off":
            _LOGGER.debug("Force-auto-fan: unit off/unavailable; nothing to do")
            return
        fan_mode = state.attributes.get("fan_mode")
        if not fan_mode:
            _LOGGER.debug("Force-auto-fan: device reports no fan_mode; skipping")
            return
        suffix = self.fan_auto_suffix.lower()
        if suffix in fan_mode.lower() or "auto" in fan_mode.lower():
            _LOGGER.debug(
                "Force-auto-fan: '%s' already carries auto suffix; no change",
                fan_mode,
            )
            return
        target = f"{fan_mode}{self.fan_auto_suffix}"
        _LOGGER.info("Force-auto-fan: setting fan mode '%s' -> '%s'", fan_mode, target)
        try:
            await self.hass.services.async_call(
                "climate",
                "set_fan_mode",
                {
                    "entity_id": self.climate_device,
                    "fan_mode": target,
                },
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001 - unit may reject the mode
            _LOGGER.warning(
                "Could not force fan mode '%s%s' on %s: %s",
                fan_mode,
                self.fan_auto_suffix,
                self.climate_device,
                err,
            )

    # ----------------------------------------------------------- fan control

    async def _async_apply_fan(self) -> None:
        """Apply whichever fan behaviour is configured (speed wins over /Auto)."""
        device_state = self._device_state()
        if (
            device_state
            and device_state.lower() == "off"
            and self._fan_base is not None
        ):
            # First off pass: put the fan back to the user's mode, then forget
            # it (mirrors the setpoint base restore).
            await self.async_restore_fan_base(clear=True)
        elif self.auto_fan_speed:
            await self._async_apply_fan_speed()
        elif self.force_auto_fan:
            await self._async_enforce_fan()
        # Refresh the hub fan-decision sensor regardless of which branch ran.
        self._notify_hub()

    def _fan_open_zones(self) -> list[dict]:
        """Per-open-zone deviation detail used by the fan logic and sensor."""
        out: list[dict] = []
        for zone in self.zones:
            switch = self.hass.states.get(zone[CONF_ZONE_SWITCH])
            if switch is None or switch.state != "on":
                continue
            target = self.get_zone_target(zone[CONF_ZONE_ID])
            current = self._read_float(zone[CONF_LOCAL_SENSOR])
            deviation = (
                abs(current - target)
                if (target is not None and current is not None)
                else None
            )
            out.append(
                {
                    "zone": zone[CONF_ZONE_NAME],
                    "current": current,
                    "target": target,
                    "deviation": round(deviation, 2) if deviation is not None else None,
                }
            )
        return out

    def _fan_demand(self) -> tuple[float, int]:
        """Worst open-zone deviation and how many zones are open.

        Deviation is the absolute distance of an open zone's room temperature
        from its target; only open zones with readable values contribute.
        """
        zones = self._fan_open_zones()
        worst = max(
            (z["deviation"] for z in zones if z["deviation"] is not None),
            default=0.0,
        )
        return worst, len(zones)

    @staticmethod
    def _fan_rank(name: str) -> float:
        """Heuristic ordering for a fan-mode name (lower = slower)."""
        lowered = name.lower()
        digits = re.search(r"\d+", lowered)
        if digits:
            return float(digits.group())
        for keyword, rank in (
            ("quiet", 0), ("silent", 0), ("night", 0), ("eco", 0), ("min", 0),
            ("low", 1),
            ("medium", 2), ("mid", 2), ("normal", 2),
            ("high", 3),
            ("max", 4), ("strong", 4), ("powerful", 4), ("turbo", 4), ("full", 4),
        ):
            if keyword in lowered:
                return rank
        return 2.0  # unknown -> middle

    def _ordered_fan_speeds(self) -> list[str]:
        """Fan modes from slowest to fastest, from config or the device."""
        configured = self._conf.get(CONF_FAN_SPEED_MODES)
        if configured:
            modes = [m.strip() for m in str(configured).split(",") if m.strip()]
            if modes:
                return modes
        state = self.hass.states.get(self.climate_device)
        fan_modes = list(state.attributes.get("fan_modes", []) if state else [])
        speeds = [
            m for m in fan_modes if m and m.lower() not in ("auto", "off")
        ]
        speeds.sort(key=self._fan_rank)
        return speeds

    def _fan_map(
        self, speeds: list[str], worst: float, open_count: int
    ) -> tuple[int, float, float, float]:
        """Map zone demand onto an index into ``speeds`` (slowest..fastest).

        Returns (index, temp_fraction, zone_bump, demand_fraction).
        """
        if open_count == 0 or len(speeds) < 2:
            return 0, 0.0, 0.0, 0.0
        temp_fraction = min(worst / self.fan_full_deviation, 1.0)
        zone_bump = min(FAN_PER_ZONE_BUMP * (open_count - 1), FAN_MAX_ZONE_BUMP)
        fraction = min(temp_fraction + zone_bump, 1.0)
        index = round(fraction * (len(speeds) - 1))
        return index, temp_fraction, zone_bump, fraction

    def _desired_fan_mode(self) -> str | None:
        """Pick a fan mode for the current demand, or None if not determinable."""
        speeds = self._ordered_fan_speeds()
        if len(speeds) < 2:
            _LOGGER.debug(
                "Auto fan speed: need >=2 speed modes, have %s; skipping", speeds
            )
            return None
        worst, open_count = self._fan_demand()
        if open_count == 0:
            _LOGGER.debug("Auto fan speed: no zones open; selecting slowest")
            return speeds[0]
        index, temp_fraction, zone_bump, fraction = self._fan_map(
            speeds, worst, open_count
        )
        _LOGGER.debug(
            "Auto fan speed: worst dev %.2f (=%.2f) + %d open zones (bump %.2f) "
            "-> demand %.2f -> index %d/%d = %s",
            worst,
            temp_fraction,
            open_count,
            zone_bump,
            fraction,
            index,
            len(speeds) - 1,
            speeds[index],
        )
        return speeds[index]

    def fan_decision(self) -> dict:
        """Explain the current auto-fan-speed decision (for the hub sensor).

        Always returns a dict; ``summary`` is the headline state and the rest
        are surfaced as attributes. ``explanation`` is an ordered, plain-language
        walk-through of how the speed was reached, and ``open_zone_details``
        lists each open zone's distance from target.
        """
        speeds = self._ordered_fan_speeds()
        device_state = self._device_state()
        mode = (device_state or "unknown").lower()
        open_zones = self._fan_open_zones()
        open_count = len(open_zones)
        worst = max(
            (z["deviation"] for z in open_zones if z["deviation"] is not None),
            default=0.0,
        )
        worst_zone = None
        if open_count:
            rated = [z for z in open_zones if z["deviation"] is not None]
            if rated:
                worst_zone = max(rated, key=lambda z: z["deviation"])["zone"]
        state = self.hass.states.get(self.climate_device)
        current_fan = state.attributes.get("fan_mode") if state else None

        detail: dict = {
            "auto_fan_speed": self.auto_fan_speed,
            "device_mode": mode,
            "open_zones": open_count,
            "open_zone_details": open_zones,
            "worst_zone": worst_zone,
            "worst_deviation": round(worst, 2),
            "ordered_speeds": speeds,
            "current_fan_mode": current_fan,
            "full_deviation_degrees": self.fan_full_deviation,
            "per_zone_bump": FAN_PER_ZONE_BUMP,
            "max_zone_bump": FAN_MAX_ZONE_BUMP,
        }

        # ---- branches where the speed isn't actually being driven ----
        if not self.auto_fan_speed:
            if self.force_auto_fan:
                detail["reason"] = "Auto fan speed off; forcing the unit's /Auto mode"
                detail["explanation"] = (
                    "Auto-fan-speed selection is off. 'Force auto fan' is on, so "
                    "SmarterZones appends the unit's /Auto suffix to the current fan "
                    "mode instead of choosing a speed."
                )
                return {"summary": "Device auto", **detail}
            detail["reason"] = "Auto fan speed is disabled"
            detail["explanation"] = (
                "Auto fan speed is turned off (options toggle or hub switch), so "
                "SmarterZones leaves the unit's fan mode untouched."
            )
            return {"summary": "Disabled", **detail}

        if len(speeds) < 2:
            detail["reason"] = "Need at least two fan speeds to choose from"
            detail["explanation"] = (
                f"Only {len(speeds)} usable fan speed(s) found "
                f"({', '.join(speeds) or 'none'}). At least two are required to scale "
                "speed with demand, so no selection is made. Define an ordered "
                "fan-speed string or check the unit's fan_modes."
            )
            return {"summary": "Unavailable", **detail}

        actively_conditioning = not (
            mode in ("off", "unavailable", "unknown")
            or "dry" in mode
            or "fan" in mode
        )
        detail["actively_conditioning"] = actively_conditioning

        index, temp_fraction, zone_bump, fraction = self._fan_map(
            speeds, worst, open_count
        )
        desired = speeds[index]
        detail.update(
            {
                "temp_fraction": round(temp_fraction, 2),
                "zone_bump": round(zone_bump, 2),
                "demand_fraction": round(fraction, 2),
                "selected_index": index,
                "speed_count": len(speeds),
                "selected_speed": desired,
            }
        )

        # ---- build the step-by-step explanation ----
        steps: list[str] = []
        if open_count == 0:
            steps.append("No zones are open.")
            steps.append(f"With nothing calling for air, the slowest speed "
                         f"'{speeds[0]}' is selected.")
            detail["reason"] = "No zones open; would use the slowest speed"
        else:
            listing = ", ".join(
                f"{z['zone']} {z['deviation']:.1f}°"
                if z["deviation"] is not None
                else f"{z['zone']} (no reading)"
                for z in open_zones
            )
            steps.append(f"{open_count} zone(s) open: {listing}.")
            if worst_zone is not None:
                steps.append(
                    f"Worst room: {worst_zone}, {worst:.1f}° from its target "
                    f"(drives the temperature demand)."
                )
            steps.append(
                f"Temperature demand: {worst:.1f}° ÷ {self.fan_full_deviation:.1f}° "
                f"(full-speed deviation) = {temp_fraction:.2f}."
            )
            steps.append(
                f"Multi-zone bump: {max(open_count - 1, 0)} extra open zone(s) × "
                f"{FAN_PER_ZONE_BUMP} = {zone_bump:.2f} "
                f"(capped at {FAN_MAX_ZONE_BUMP})."
            )
            steps.append(
                f"Total demand: {temp_fraction:.2f} + {zone_bump:.2f} = "
                f"{fraction:.2f} (capped at 1.00)."
            )
            steps.append(
                f"Mapped across {len(speeds)} speeds "
                f"({' < '.join(speeds)}): index {index} → '{desired}'."
            )
            detail["reason"] = (
                f"Worst room is {worst:.1f}° from target across {open_count} "
                f"open zone(s) -> demand {fraction:.0%} -> {desired}"
            )

        if not actively_conditioning:
            steps.append(
                f"Unit is '{mode}', not actively heating/cooling, so the fan speed "
                f"is left alone (this would be '{desired}' if it were conditioning)."
            )
            detail["would_change"] = False
            detail["explanation"] = " ".join(steps)
            return {"summary": f"Idle ({mode})", **detail}

        if current_fan == desired:
            steps.append(f"Unit is already on '{desired}'; no change needed.")
        else:
            steps.append(f"Unit fan is '{current_fan}' → changing to '{desired}'.")
        detail["would_change"] = current_fan != desired
        detail["explanation"] = " ".join(steps)
        return {"summary": desired, **detail}

    async def _async_apply_fan_speed(self) -> None:
        """Set the fan speed from zone demand, only while actively conditioning."""
        device_state = self._device_state()
        if device_state is None:
            _LOGGER.debug("Auto fan speed: climate device unavailable; skipping")
            return
        state_l = device_state.lower()
        if (
            state_l in ("off", "unavailable", "unknown")
            or "dry" in state_l
            or "fan" in state_l
        ):
            _LOGGER.debug(
                "Auto fan speed: not actively conditioning (mode=%s); skipping",
                state_l,
            )
            return
        desired = self._desired_fan_mode()
        if not desired:
            return
        state = self.hass.states.get(self.climate_device)
        current_fan = state.attributes.get("fan_mode") if state else None
        if current_fan == desired:
            _LOGGER.debug(
                "Auto fan speed: already at '%s'; no change", desired
            )
            return
        # Remember the user's fan mode just before the first time we change it,
        # in case it wasn't captured at power-on (e.g. the unit was off then).
        if self._fan_base is None and current_fan:
            self._fan_base = current_fan
            _LOGGER.info(
                "Auto fan speed: remembered base fan mode '%s' (first change)",
                current_fan,
            )
        if await self._async_climate_call(
            "set_fan_mode", {"fan_mode": desired}, f"set fan mode '{desired}'"
        ):
            self._last_commanded_fan = desired
            worst, open_count = self._fan_demand()
            _LOGGER.info(
                "Auto fan speed: '%s' -> '%s' (worst deviation %.1f, %d zone(s) open)",
                current_fan,
                desired,
                worst,
                open_count,
            )

    # ------------------------------------------------------- setpoint control

    def _setpoint_demand(self, direction: str) -> dict:
        """Worst unmet room demand among open zones, for a cool/heat direction.

        Demand is the *signed* distance a room still has to travel toward its
        target in the conditioning direction: for cooling, current - target
        (positive while too warm); for heating, target - current. Only currently
        open zones contribute; an open zone whose sensor can't be read is flagged
        so we keep conditioning rather than guess it's satisfied.
        """
        detail: list[dict] = []
        worst = 0.0
        open_count = 0
        unreadable = False
        for zone in self.zones:
            switch = self.hass.states.get(zone[CONF_ZONE_SWITCH])
            if switch is None or switch.state != "on":
                continue
            open_count += 1
            target = self.get_zone_target(zone[CONF_ZONE_ID])
            current = self._read_float(zone[CONF_LOCAL_SENSOR])
            if target is None or current is None:
                unreadable = True
                demand = None
            else:
                demand = (current - target) if direction == "cool" else (target - current)
                if demand > worst:
                    worst = demand
            detail.append(
                {
                    "zone": zone[CONF_ZONE_NAME],
                    "current": current,
                    "target": target,
                    "demand": round(demand, 2) if demand is not None else None,
                }
            )
        return {
            "worst": worst,
            "open_count": open_count,
            "unreadable": unreadable,
            "zones": detail,
        }

    def _setpoint_eval(self) -> dict:
        """Work out the setpoint the auto-setpoint feature would request.

        Anchors to the unit's *own* current_temperature, because that is the
        reading the air conditioner compares its setpoint against when deciding
        whether to run - even when that reading disagrees with the rooms. The
        room sensors only decide whether to keep biasing and by how much. Only
        single-setpoint cool/heat modes are handled (heat_cool uses a band and
        is left alone). The returned dict always carries a ``status`` headline
        plus the inputs behind it; ``desired_setpoint`` is present only when a
        value is actually being driven.
        """
        unit = self.hass.config.units.temperature_unit
        out: dict = {
            "auto_setpoint": self.auto_setpoint,
            "max_bias": round(self.setpoint_max_bias, 2),
            "min_bias": SETPOINT_MIN_BIAS,
            "full_deviation_degrees": SETPOINT_FULL_DEVIATION,
        }
        state = self.hass.states.get(self.climate_device)
        device_state = state.state if state else None
        mode = (device_state or "unknown").lower()
        out["device_mode"] = mode

        if not self.auto_setpoint:
            out["status"] = "Disabled"
            out["reason"] = "Auto target temperature is turned off"
            return out

        if "cool" in mode and "heat" not in mode:
            direction = "cool"
        elif "heat" in mode and "cool" not in mode:
            direction = "heat"
        else:
            out["status"] = f"Idle ({mode})"
            out["reason"] = (
                "Auto target temperature only acts in a single-setpoint cool or "
                f"heat mode; unit is '{mode}'."
            )
            return out
        out["direction"] = direction

        unit_temp = state.attributes.get("current_temperature")
        try:
            unit_temp = float(unit_temp) if unit_temp is not None else None
        except (TypeError, ValueError):
            unit_temp = None
        if unit_temp is None:
            out["status"] = "Unavailable"
            out["reason"] = (
                "Unit reports no current_temperature to anchor the setpoint to"
            )
            return out
        out["unit_temperature"] = round(unit_temp, 2)

        current_setpoint = state.attributes.get("temperature")
        try:
            current_setpoint = (
                float(current_setpoint) if current_setpoint is not None else None
            )
        except (TypeError, ValueError):
            current_setpoint = None
        out["current_setpoint"] = current_setpoint

        # Guardrails come straight from the climate device so we never request a
        # setpoint it would reject: snap to its target_temp_step and clamp to its
        # min_temp/max_temp.
        step = state.attributes.get("target_temp_step") or TARGET_TEMP_STEP
        try:
            step = float(step)
        except (TypeError, ValueError):
            step = TARGET_TEMP_STEP
        if step <= 0:
            step = TARGET_TEMP_STEP

        def _coerce(value):
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        min_t = _coerce(state.attributes.get("min_temp"))
        max_t = _coerce(state.attributes.get("max_temp"))
        if min_t is not None and max_t is not None and min_t > max_t:
            min_t, max_t = max_t, min_t
        out["device_min_temp"] = min_t
        out["device_max_temp"] = max_t
        out["device_step"] = step

        demand = self._setpoint_demand(direction)
        worst = demand["worst"]
        open_count = demand["open_count"]
        out["open_zones"] = open_count
        out["open_zone_details"] = demand["zones"]
        out["worst_demand"] = round(worst, 2)

        temp_fraction = min(worst / SETPOINT_FULL_DEVIATION, 1.0) if worst > 0 else 0.0
        zone_bump = min(
            SETPOINT_PER_ZONE_BUMP * max(open_count - 1, 0), SETPOINT_MAX_ZONE_BUMP
        )
        fraction = min(temp_fraction + zone_bump, 1.0)
        out["demand_fraction"] = round(fraction, 2)

        demand_present = worst > 0 or demand["unreadable"]
        out["demand_present"] = demand_present
        if demand_present:
            span = max(self.setpoint_max_bias - SETPOINT_MIN_BIAS, 0.0)
            bias = min(SETPOINT_MIN_BIAS + fraction * span, self.setpoint_max_bias)
            raw = unit_temp - bias if direction == "cool" else unit_temp + bias
            out["bias"] = round(bias, 2)
        else:
            raw = (
                unit_temp + SETPOINT_REST_MARGIN
                if direction == "cool"
                else unit_temp - SETPOINT_REST_MARGIN
            )
            out["bias"] = -round(SETPOINT_REST_MARGIN, 2)

        # The user's setpoint is a hard bound: while cooling the target is never
        # set above it, while heating never below it. The bias only ever pushes
        # *past* the user's level (to defeat the unit's optimistic return-air
        # reading), never short of it - the unit-anchored value alone could land
        # on the wrong side of the base when the return air reads extreme.
        base = (
            self._setpoint_base if self._setpoint_base is not None else current_setpoint
        )
        out["base_setpoint"] = base
        if base is not None:
            bounded = min(raw, base) if direction == "cool" else max(raw, base)
            out["base_bounded"] = bounded != raw
            raw = bounded
        else:
            out["base_bounded"] = False

        desired = round(raw / step) * step
        if min_t is not None:
            desired = max(min_t, desired)
        if max_t is not None:
            desired = min(max_t, desired)
        desired = round(desired, 2)
        out["clamped"] = (
            (min_t is not None and raw < min_t)
            or (max_t is not None and raw > max_t)
        )
        out["desired_setpoint"] = desired
        out["actively_conditioning"] = True
        out["status"] = f"{desired:g} {unit}"
        return out

    async def _async_apply_setpoint(self) -> None:
        """Bias the unit's setpoint from room demand, while cool/heat conditioning."""
        plan = self._setpoint_eval()
        # Refresh the hub setpoint-decision sensor regardless of the outcome.
        self._notify_hub()
        if not plan.get("auto_setpoint"):
            return
        desired = plan.get("desired_setpoint")
        if desired is None:
            # Not actively conditioning. The first time the unit is found off we put
            # the setpoint back to the user's base, then clear our memory of it - so
            # we don't keep writing to the unit while it stays off. The base is
            # re-captured the next time the unit is turned on.
            if (
                (plan.get("device_mode") or "").lower() == "off"
                and self._setpoint_base is not None
            ):
                await self.async_restore_setpoint_base(clear=True)
            else:
                _LOGGER.debug(
                    "Auto setpoint: not applied (%s)",
                    plan.get("reason", plan.get("status")),
                )
            return
        current_setpoint = plan.get("current_setpoint")
        if (
            current_setpoint is not None
            and abs(desired - current_setpoint) < SETPOINT_DEADBAND
        ):
            _LOGGER.debug(
                "Auto setpoint: already ~%.1f° (current %.1f°); no change",
                desired,
                current_setpoint,
            )
            return
        # Remember the user's setpoint just before the first time we change it, in
        # case it wasn't captured at switch-on (e.g. the unit was off back then).
        if self._setpoint_base is None and current_setpoint is not None:
            self._setpoint_base = current_setpoint
            _LOGGER.info(
                "Auto target temperature: remembered base setpoint %.1f° (first bias)",
                current_setpoint,
            )
        if await self._async_climate_call(
            "set_temperature",
            {"temperature": desired},
            f"set target temperature {desired:.1f}°",
        ):
            self._last_commanded_setpoint = desired
            _LOGGER.info(
                "Auto setpoint: %s -> %.1f° (%s; worst demand %.1f° over %d open "
                "zone(s), unit reads %.1f°)",
                current_setpoint,
                desired,
                plan.get("direction"),
                plan.get("worst_demand", 0.0),
                plan.get("open_zones", 0),
                plan.get("unit_temperature", 0.0),
            )

    def setpoint_decision(self) -> dict:
        """Explain the current auto-setpoint decision (for the hub sensor).

        Returns a dict whose ``summary`` is the headline (the requested setpoint
        or a status such as Disabled/Idle/Unavailable) and whose remaining keys
        are surfaced as attributes, including a plain-language ``explanation``.
        """
        plan = self._setpoint_eval()
        summary = plan.pop("status", "Unknown")

        steps: list[str] = []
        if "desired_setpoint" in plan:
            direction = plan["direction"]
            verb = "cooling" if direction == "cool" else "heating"
            steps.append(
                f"Unit is {verb}; its own sensor reads "
                f"{plan['unit_temperature']:.1f}° (the value it regulates against)."
            )
            zones = plan.get("open_zone_details", [])
            if plan.get("open_zones"):
                listing = ", ".join(
                    f"{z['zone']} {z['demand']:+.1f}°"
                    if z["demand"] is not None
                    else f"{z['zone']} (no reading)"
                    for z in zones
                )
                steps.append(f"{plan['open_zones']} open zone(s): {listing}.")
            else:
                steps.append("No zones are open.")
            if plan.get("demand_present"):
                side = "below" if direction == "cool" else "above"
                steps.append(
                    f"Worst unmet demand {plan['worst_demand']:.1f}° → demand "
                    f"{plan['demand_fraction']:.0%}, biasing the setpoint "
                    f"{abs(plan['bias']):.1f}° {side} the unit's reading."
                )
            else:
                steps.append(
                    "All open zones are satisfied; easing the setpoint back so the "
                    "unit can idle instead of over-conditioning."
                )
            if plan.get("base_bounded"):
                side = "above" if direction == "cool" else "below"
                steps.append(
                    f"Held at your setpoint {plan['base_setpoint']:g}° - the target "
                    f"is never {side} it while {verb}."
                )
            if plan.get("clamped"):
                lo = plan.get("device_min_temp")
                hi = plan.get("device_max_temp")
                if lo is not None and hi is not None:
                    bounds = f"[{lo:g}°, {hi:g}°]"
                elif lo is not None:
                    bounds = f"≥ {lo:g}°"
                else:
                    bounds = f"≤ {hi:g}°"
                steps.append(
                    f"Held within the unit's limits {bounds} and snapped to its "
                    f"{plan['device_step']:g}° step."
                )
            change = (
                plan.get("current_setpoint") is None
                or abs(plan["desired_setpoint"] - plan["current_setpoint"])
                >= SETPOINT_DEADBAND
            )
            tail = "" if change else " (already there; no change)"
            steps.append(f"Requested setpoint: {plan['desired_setpoint']:g}°{tail}.")
            plan["would_change"] = change

        plan["explanation"] = " ".join(steps) if steps else plan.get("reason", "")
        plan["summary"] = summary
        return plan

    async def _handle_zone_entity_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        entity_id = event.data["entity_id"]
        new = event.data.get("new_state")
        affected: list[dict] = []
        for zone in self.zones:
            relevant = entity_id == zone[CONF_LOCAL_SENSOR] or entity_id in {
                c[CONF_CONDITION_ENTITY] for c in zone.get(CONF_CONDITIONS, [])
            }
            if relevant:
                affected.append(zone)
        if affected:
            _LOGGER.debug(
                "%s changed to '%s'; re-evaluating zone(s): %s",
                entity_id,
                new.state if new else "unknown",
                ", ".join(z[CONF_ZONE_NAME] for z in affected),
            )
        # One reconciled pass over the affected zones (plus the common zone), so no
        # damper is commanded more than once. Status is refreshed inside.
        await self._async_apply_zone_decisions(affected, turn_on=False)
        await self._async_apply_fan()
        await self._async_apply_setpoint()

    async def _handle_trigger_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        try:
            temp = float(new_state.state)
        except (TypeError, ValueError):
            return
        high = float(self._conf.get(CONF_TRIGGER_HIGH, DEFAULT_TRIGGER_HIGH))
        low = float(self._conf.get(CONF_TRIGGER_LOW, DEFAULT_TRIGGER_LOW))
        if temp > high:
            mode = "cool"
        elif temp < low:
            mode = "heat"
        else:
            _LOGGER.debug(
                "Trigger temp %.1f within neutral band [%.1f, %.1f]; no power-on",
                temp,
                low,
                high,
            )
            return
        _LOGGER.info("Trigger temp %s -> powering on in %s mode", temp, mode)
        await self.hass.services.async_call(
            "climate", "turn_on", {"entity_id": self.climate_device}, blocking=True
        )
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self.climate_device, "hvac_mode": mode},
            blocking=False,
        )

    # ----------------------------------------------------------- zone logic

    async def async_manage_all(self, turn_on: bool = False) -> None:
        await self._async_apply_zone_decisions(self.zones, turn_on=turn_on)
        await self._async_apply_fan()
        await self._async_apply_setpoint()

    async def async_manage_zone(self, zone: dict, turn_on: bool = False) -> None:
        """Re-evaluate a single zone (and reconcile the common zone) in one pass."""
        await self._async_apply_zone_decisions([zone], turn_on=turn_on)

    async def _async_apply_zone_decisions(
        self, zones: list[dict], turn_on: bool = False
    ) -> None:
        """Decide every given zone (plus the common zone) once, reconcile, and apply
        each damper a single time.

        Keeping the decision (``_decide_zone``/``_decide_common``, pure functions)
        separate from application means two rules can never command the same switch
        twice in one pass - which is what caused the open/close bounce on turn-on.
        ``None`` means "hold as-is" (no command), so hysteresis holds are preserved.
        """
        desired: dict[str, tuple[bool, str] | None] = {}
        for zone in zones:
            desired[zone[CONF_ZONE_SWITCH]] = self._decide_zone(zone, turn_on=turn_on)

        # The common zone has a single authority: its airflow/comfort decision
        # overrides the per-zone hysteresis for that damper when it has an opinion.
        common = self.common_zone_switch
        if common:
            common_decision = self._decide_common(desired)
            if common_decision is not None:
                desired[common] = common_decision

        # Apply once: other zones first, then the common zone (open/close the shared
        # airflow path after the rest are settled).
        for switch, decision in desired.items():
            if switch == common or decision is None:
                continue
            await self._async_set_switch(switch, decision[0], reason=decision[1])
        if common and desired.get(common) is not None:
            await self._async_set_switch(
                common, desired[common][0], reason=desired[common][1]
            )

        for zone in zones:
            self._notify_status(zone[CONF_ZONE_ID])

    def _decide_zone(
        self, zone: dict, turn_on: bool = False
    ) -> tuple[bool, str] | None:
        """Pure open/close decision for one zone (no side effects).

        Returns ``(open, reason)`` to command the damper, or ``None`` to hold it as
        it is (in-band hysteresis, not ready, manual control, climate unavailable).
        """
        zone_id = zone[CONF_ZONE_ID]
        name = zone[CONF_ZONE_NAME]
        if not self._is_ready(zone_id):
            _LOGGER.debug("%s: not ready (target/switch not registered); holding", name)
            return None
        switch = zone[CONF_ZONE_SWITCH]
        # The common zone's "open to keep an airflow path" decision is owned by
        # _decide_common; don't let the generic turn-on also open it here.
        if self.common_zone_switch and switch == self.common_zone_switch:
            turn_on = False
        device_state = self._device_state()
        if device_state is None:
            _LOGGER.debug(
                "%s: climate device %s unavailable; holding", name, self.climate_device
            )
            return None
        state_l = device_state.lower()
        enabled = self.is_zone_enabled(zone_id)
        _LOGGER.debug(
            "%s: deciding (device=%s, smart_control=%s, turn_on=%s)",
            name,
            state_l,
            "on" if enabled else "off",
            turn_on,
        )

        # Dry / fan: just open the zone unless the user disabled smart control.
        if "dry" in state_l or "fan" in state_l:
            if enabled:
                return (True, f"{state_l} mode opens all zones")
            _LOGGER.debug("%s: %s mode but smart control off; manual", name, state_l)
            return None

        if state_l == "off":
            return (False, "climate device off")

        if not enabled:
            _LOGGER.debug("%s: smart control off; leaving under manual control", name)
            return None

        mode = self._mode(device_state)
        if mode in (ACMode.HEATING, ACMode.COOLING, ACMode.HEAT_COOL):
            # Debounced: a brief condition flap (door opened and shut again)
            # moves no dampers; see _effective_conditions_met.
            if not self._effective_conditions_met(zone):
                return (False, "a condition is not met")
            _LOGGER.debug("%s: all conditions met", name)

        target = self.get_zone_target(zone_id)
        if target is None:
            _LOGGER.debug("%s: target temperature not set yet; holding", name)
            return None

        current = self._read_float(zone[CONF_LOCAL_SENSOR])
        if current is None:
            _LOGGER.warning(
                "%s: local sensor %s unreadable; opening zone as a fail-safe",
                name,
                zone[CONF_LOCAL_SENSOR],
            )
            return (True, "local sensor unreadable (fail-safe)")

        upper, lower = self._offsets(zone, device_state)
        max_temp = round(target + upper, 2)
        min_temp = round(target - lower, 2)
        _LOGGER.debug(
            "%s: mode=%s current=%.2f target=%.2f band=[%.2f, %.2f] (offsets +%.2f/-%.2f)",
            name,
            mode.name,
            current,
            target,
            min_temp,
            max_temp,
            upper,
            lower,
        )

        if mode == ACMode.COOLING:
            if current >= max_temp:
                return (True, f"cooling: {current:.2f} >= max {max_temp:.2f} (too warm)")
            if current <= min_temp:
                return (False, f"cooling: {current:.2f} <= min {min_temp:.2f} (cool enough)")
            if turn_on:
                return (True, "cooling: turn-on, room within band")
            _LOGGER.debug(
                "%s: cooling: %.2f within band [%.2f, %.2f]; holding",
                name, current, min_temp, max_temp,
            )
            return None
        if mode == ACMode.HEATING:
            if current <= min_temp:
                return (True, f"heating: {current:.2f} <= min {min_temp:.2f} (too cold)")
            if current >= max_temp:
                return (False, f"heating: {current:.2f} >= max {max_temp:.2f} (warm enough)")
            if turn_on:
                return (True, "heating: turn-on, room within band")
            _LOGGER.debug(
                "%s: heating: %.2f within band [%.2f, %.2f]; holding",
                name, current, min_temp, max_temp,
            )
            return None
        if mode == ACMode.HEAT_COOL:
            if min_temp < current < max_temp:
                return (False, f"heat_cool: {current:.2f} within band (comfortable)")
            return (
                True,
                f"heat_cool: {current:.2f} outside band [{min_temp:.2f}, {max_temp:.2f}]",
            )
        if mode == ACMode.OFF:
            return (False, "mode off")
        _LOGGER.debug("%s: mode %s not actively managed; holding", name, mode.name)
        return None

    def _decide_common(
        self, desired: dict[str, tuple[bool, str] | None]
    ) -> tuple[bool, str] | None:
        """Pure decision for the common (airflow-path) zone.

        Uses the *post-pass effective* state of the other zones - their decision
        this pass if they have one, otherwise their live state - so it reacts to
        what the dampers will be, not what they currently are. Returns
        ``(open, reason)``, or ``None`` to defer to its own per-zone hysteresis
        decision / leave it as-is.
        """
        common = self.common_zone_switch
        device_state = self._device_state()
        state_l = device_state.lower() if device_state else None
        if state_l == "off":
            return (False, "climate device off")
        if device_state is None or state_l in ("unavailable", "unknown"):
            _LOGGER.debug("Common zone %s: unit unavailable; holding", common)
            return None

        def effective_open(switch: str) -> bool:
            decision = desired.get(switch)
            if decision is not None:
                return decision[0]
            st = self.hass.states.get(switch)
            return st is not None and st.state == "on"

        any_other_open = any(
            effective_open(z[CONF_ZONE_SWITCH])
            for z in self.zones
            if z[CONF_ZONE_SWITCH] != common
        )
        if not any_other_open:
            return (True, "no other zone open - keep an airflow path")

        common_zone = next(
            (z for z in self.zones if z[CONF_ZONE_SWITCH] == common), None
        )
        if common_zone is None:
            _LOGGER.debug(
                "Common zone %s isn't a configured zone; leaving open while others run",
                common,
            )
            return None
        temp = self._read_float(common_zone[CONF_LOCAL_SENSOR])
        target = self.get_zone_target(common_zone[CONF_ZONE_ID])
        if temp is None or target is None:
            _LOGGER.debug("Common zone %s: temp/target unknown; holding", common)
            return None
        upper, lower = self._offsets(common_zone, device_state)
        if (target - lower) < temp < (target + upper):
            return (False, f"comfortable ({temp:.2f} in band) and other zones open")
        # Not comfortable: defer to its own hysteresis decision (too warm/cold opens).
        _LOGGER.debug(
            "Common zone %s: %.2f outside band [%.2f, %.2f]; deferring to hysteresis",
            common,
            temp,
            target - lower,
            target + upper,
        )
        return None
