"""A proxy climate entity that surfaces the AC controls on the controller device.

This mirrors the underlying (e.g. Daikin) climate entity and forwards any
control changes back to it, so the air conditioner can be operated directly
from the SmarterZones device. It does not change how zones are decided - the
manager still reads the same underlying climate entity.
"""

from __future__ import annotations

import logging

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from . import SmarterZonesConfigEntry
from .const import CONF_CLIMATE_DEVICE, CONF_EXPOSE_CLIMATE
from .entity import hub_device_info

_LOGGER = logging.getLogger(__name__)

# Climate state-attribute keys we read from the source entity.
ATTR_CURRENT_TEMP = "current_temperature"
ATTR_TEMP = "temperature"
ATTR_TEMP_HIGH = "target_temp_high"
ATTR_TEMP_LOW = "target_temp_low"
ATTR_TEMP_STEP = "target_temp_step"
ATTR_MIN_TEMP = "min_temp"
ATTR_MAX_TEMP = "max_temp"
ATTR_HVAC_MODES = "hvac_modes"
ATTR_HVAC_ACTION = "hvac_action"
ATTR_FAN_MODE = "fan_mode"
ATTR_FAN_MODES = "fan_modes"
ATTR_SWING_MODE = "swing_mode"
ATTR_SWING_MODES = "swing_modes"
ATTR_PRESET_MODE = "preset_mode"
ATTR_PRESET_MODES = "preset_modes"
ATTR_SUPPORTED_FEATURES = "supported_features"

_UNAVAILABLE = {"unavailable", "unknown"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmarterZonesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    conf = {**entry.data, **entry.options}
    if not conf.get(CONF_EXPOSE_CLIMATE, True):
        return
    async_add_entities(
        [SmarterZonesClimate(entry, conf[CONF_CLIMATE_DEVICE])]
    )


class SmarterZonesClimate(ClimateEntity):
    """Mirror of the underlying climate entity, attached to the hub device."""

    _attr_has_entity_name = True
    _attr_name = "Air conditioner"
    _attr_should_poll = False
    _attr_icon = "mdi:air-conditioner"

    def __init__(self, entry: SmarterZonesConfigEntry, source: str) -> None:
        self._source = source
        self._attr_unique_id = f"{entry.entry_id}_climate"
        self._attr_device_info = hub_device_info(entry.entry_id, entry.title)

    # ----------------------------------------------------------- source access

    @property
    def _state(self):
        return self.hass.states.get(self._source)

    def _attr_value(self, key: str):
        state = self._state
        return state.attributes.get(key) if state else None

    @property
    def available(self) -> bool:
        state = self._state
        return state is not None and state.state not in _UNAVAILABLE

    @property
    def temperature_unit(self) -> str:
        # Climate state temperatures are normalised to the system unit.
        return self.hass.config.units.temperature_unit

    # ------------------------------------------------------------- read-through

    @property
    def hvac_mode(self) -> HVACMode | None:
        state = self._state
        if state is None or state.state in _UNAVAILABLE:
            return None
        try:
            return HVACMode(state.state)
        except ValueError:
            return None

    @property
    def hvac_modes(self) -> list[HVACMode]:
        modes = self._attr_value(ATTR_HVAC_MODES) or []
        result: list[HVACMode] = []
        for mode in modes:
            try:
                result.append(HVACMode(mode))
            except ValueError:
                continue
        return result

    @property
    def hvac_action(self) -> HVACAction | None:
        action = self._attr_value(ATTR_HVAC_ACTION)
        if action is None:
            return None
        try:
            return HVACAction(action)
        except ValueError:
            return None

    @property
    def current_temperature(self):
        return self._attr_value(ATTR_CURRENT_TEMP)

    @property
    def target_temperature(self):
        return self._attr_value(ATTR_TEMP)

    @property
    def target_temperature_high(self):
        return self._attr_value(ATTR_TEMP_HIGH)

    @property
    def target_temperature_low(self):
        return self._attr_value(ATTR_TEMP_LOW)

    @property
    def target_temperature_step(self):
        return self._attr_value(ATTR_TEMP_STEP)

    @property
    def min_temp(self):
        value = self._attr_value(ATTR_MIN_TEMP)
        return value if value is not None else super().min_temp

    @property
    def max_temp(self):
        value = self._attr_value(ATTR_MAX_TEMP)
        return value if value is not None else super().max_temp

    @property
    def fan_mode(self):
        return self._attr_value(ATTR_FAN_MODE)

    @property
    def fan_modes(self):
        return self._attr_value(ATTR_FAN_MODES)

    @property
    def swing_mode(self):
        return self._attr_value(ATTR_SWING_MODE)

    @property
    def swing_modes(self):
        return self._attr_value(ATTR_SWING_MODES)

    @property
    def preset_mode(self):
        return self._attr_value(ATTR_PRESET_MODE)

    @property
    def preset_modes(self):
        return self._attr_value(ATTR_PRESET_MODES)

    @property
    def supported_features(self) -> ClimateEntityFeature:
        raw = self._attr_value(ATTR_SUPPORTED_FEATURES) or 0
        try:
            features = ClimateEntityFeature(int(raw))
        except ValueError:
            features = ClimateEntityFeature(0)
        # Always allow turning the unit on/off from this entity.
        return features | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF

    # ----------------------------------------------------------- write-through

    async def _call(self, service: str, **data) -> None:
        _LOGGER.info(
            "Proxy climate: forwarding %s%s to %s",
            service,
            f" {data}" if data else "",
            self._source,
        )
        await self.hass.services.async_call(
            "climate",
            service,
            {"entity_id": self._source, **data},
            blocking=True,
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._call("set_hvac_mode", hvac_mode=hvac_mode)

    async def async_set_temperature(self, **kwargs) -> None:
        data = {
            key: kwargs[key]
            for key in (ATTR_TEMP, ATTR_TEMP_HIGH, ATTR_TEMP_LOW, "hvac_mode")
            if kwargs.get(key) is not None
        }
        if data:
            await self._call("set_temperature", **data)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._call("set_fan_mode", fan_mode=fan_mode)

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        await self._call("set_swing_mode", swing_mode=swing_mode)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self._call("set_preset_mode", preset_mode=preset_mode)

    async def async_turn_on(self) -> None:
        await self._call("turn_on")

    async def async_turn_off(self) -> None:
        await self._call("turn_off")

    # --------------------------------------------------------------- tracking

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._source], self._handle_source_change
            )
        )

    @callback
    def _handle_source_change(self, event) -> None:
        self.async_write_ha_state()
