"""Per-zone number entities: target temperature and the four offsets.

These are owned by the integration (no external input_number helpers needed),
restored across restarts, and pushed to the control manager whenever they
change so adjustments take effect immediately.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SmarterZonesConfigEntry
from .const import (
    CONF_COOL_LOWER,
    CONF_COOL_UPPER,
    CONF_FAN_FULL_DEVIATION,
    CONF_HEAT_LOWER,
    CONF_HEAT_UPPER,
    CONF_SETPOINT_MAX_BIAS,
    CONF_TARGET_INIT,
    CONF_ZONES,
    DEFAULT_OFFSET,
    DEFAULT_TARGET_TEMP,
    FAN_FULL_DEVIATION,
    SETPOINT_BIAS_MAX,
    SETPOINT_BIAS_MIN,
    SETPOINT_BIAS_STEP,
    SETPOINT_MAX_BIAS,
    TARGET_TEMP_MAX,
    TARGET_TEMP_MIN,
    TARGET_TEMP_STEP,
)
from .entity import SmarterZonesZoneEntity, hub_device_info

_LOGGER = logging.getLogger(__name__)

# (config key, label, icon) for each adjustable offset.
OFFSET_SPECS: tuple[tuple[str, str, str], ...] = (
    (CONF_COOL_UPPER, "Cooling upper offset", "mdi:thermometer-chevron-up"),
    (CONF_COOL_LOWER, "Cooling lower offset", "mdi:thermometer-chevron-down"),
    (CONF_HEAT_UPPER, "Heating upper offset", "mdi:thermometer-chevron-up"),
    (CONF_HEAT_LOWER, "Heating lower offset", "mdi:thermometer-chevron-down"),
)

OFFSET_MIN = 0.0
OFFSET_MAX = 10.0
OFFSET_STEP = 0.1

FAN_DEVIATION_MIN = 0.5
FAN_DEVIATION_MAX = 10.0
FAN_DEVIATION_STEP = 0.5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmarterZonesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager = entry.runtime_data
    zones = {**entry.data, **entry.options}.get(CONF_ZONES, [])
    entities: list = []
    for zone in zones:
        entities.append(ZoneTargetNumber(manager, entry.entry_id, zone))
        for key, label, icon in OFFSET_SPECS:
            entities.append(
                ZoneOffsetNumber(manager, entry.entry_id, zone, key, label, icon)
            )
    # Hub-level tuning for auto fan speed (only when the fan feature is in play).
    if manager.fan_speed_modes_defined or manager.auto_fan_speed:
        entities.append(FanFullDeviationNumber(manager, entry.entry_id, entry.title))
    # Hub-level tuning for auto target temperature (always available, like the
    # auto-fan-speed tuning).
    entities.append(SetpointBiasNumber(manager, entry.entry_id, entry.title))
    async_add_entities(entities)


class ZoneTargetNumber(SmarterZonesZoneEntity, RestoreNumber):
    """The desired temperature for a zone."""

    _attr_name = "Target temperature"
    _attr_icon = "mdi:thermometer"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = TARGET_TEMP_MIN
    _attr_native_max_value = TARGET_TEMP_MAX
    _attr_native_step = TARGET_TEMP_STEP
    _attr_mode = NumberMode.BOX

    def __init__(self, manager, entry_id: str, zone: dict) -> None:
        super().__init__(manager, entry_id, zone, "target_temperature")
        self._attr_native_value = float(
            zone.get(CONF_TARGET_INIT, DEFAULT_TARGET_TEMP)
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) is not None:
            if last.native_value is not None:
                self._attr_native_value = last.native_value
        self._manager.set_zone_target(self._zone_id, self._attr_native_value)
        if (zone := self._zone) is not None:
            await self._manager.async_manage_zone(zone)

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info(
            "Target temperature for zone '%s' set to %.1f", self._attr_name, value
        )
        self._attr_native_value = value
        self._manager.set_zone_target(self._zone_id, value)
        self.async_write_ha_state()
        if (zone := self._zone) is not None:
            await self._manager.async_manage_zone(zone)


class ZoneOffsetNumber(SmarterZonesZoneEntity, RestoreNumber):
    """One adjustable temperature offset for a zone (e.g. cooling upper)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = OFFSET_MIN
    _attr_native_max_value = OFFSET_MAX
    _attr_native_step = OFFSET_STEP
    _attr_mode = NumberMode.BOX

    def __init__(
        self, manager, entry_id: str, zone: dict, key: str, label: str, icon: str
    ) -> None:
        super().__init__(manager, entry_id, zone, f"offset_{key}")
        self._key = key
        self._attr_name = label
        self._attr_icon = icon
        self._attr_native_value = float(zone.get(key, DEFAULT_OFFSET))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) is not None:
            if last.native_value is not None:
                self._attr_native_value = last.native_value
        self._manager.set_zone_offset(self._zone_id, self._key, self._attr_native_value)
        if (zone := self._zone) is not None:
            await self._manager.async_manage_zone(zone)

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.debug(
            "Offset '%s' for zone '%s' set to %.2f",
            self._attr_name,
            self._zone_id,
            value,
        )
        self._attr_native_value = value
        self._manager.set_zone_offset(self._zone_id, self._key, value)
        self.async_write_ha_state()
        if (zone := self._zone) is not None:
            await self._manager.async_manage_zone(zone)


class FanFullDeviationNumber(RestoreNumber):
    """Hub control: degrees the worst open zone is from target that maps to full fan speed.

    Lower values make the fan ramp to full speed sooner; higher values make it
    hold lower speeds for longer. Restored across restarts and pushed to the
    manager so auto fan speed recalculates immediately.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Fan full-speed deviation"
    _attr_icon = "mdi:fan-chevron-up"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = FAN_DEVIATION_MIN
    _attr_native_max_value = FAN_DEVIATION_MAX
    _attr_native_step = FAN_DEVIATION_STEP
    _attr_mode = NumberMode.BOX

    def __init__(self, manager, entry_id: str, title: str) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry_id}_{CONF_FAN_FULL_DEVIATION}"
        self._attr_device_info = hub_device_info(entry_id, title)
        self._attr_native_value = float(FAN_FULL_DEVIATION)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) is not None:
            if last.native_value is not None:
                self._attr_native_value = last.native_value
        self._manager.set_fan_full_deviation(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("Fan full-speed deviation set to %.1f", value)
        self._attr_native_value = value
        self._manager.set_fan_full_deviation(value)
        self.async_write_ha_state()
        await self._manager.async_refresh_fan()


class SetpointBiasNumber(RestoreNumber):
    """Hub control: max degrees the unit's setpoint is pushed past its own reading.

    At full room demand the auto target-temperature feature offsets the climate
    setpoint this many degrees beyond the unit's current_temperature (below it
    when cooling, above when heating) so the unit keeps conditioning until the
    real rooms are satisfied. Lower values are gentler; higher values push
    harder. Restored across restarts and pushed to the manager so the setpoint
    recalculates immediately.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Setpoint bias"
    _attr_icon = "mdi:thermometer-chevron-up"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = SETPOINT_BIAS_MIN
    _attr_native_max_value = SETPOINT_BIAS_MAX
    _attr_native_step = SETPOINT_BIAS_STEP
    _attr_mode = NumberMode.BOX

    def __init__(self, manager, entry_id: str, title: str) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry_id}_{CONF_SETPOINT_MAX_BIAS}"
        self._attr_device_info = hub_device_info(entry_id, title)
        self._attr_native_value = float(SETPOINT_MAX_BIAS)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) is not None:
            if last.native_value is not None:
                self._attr_native_value = last.native_value
        self._manager.set_setpoint_max_bias(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("Setpoint bias set to %.1f", value)
        self._attr_native_value = value
        self._manager.set_setpoint_max_bias(value)
        self.async_write_ha_state()
        await self._manager.async_refresh_setpoint()
