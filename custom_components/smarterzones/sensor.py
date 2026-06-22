"""Sensor platform for SmarterZones.

- Projected status: Open/Closed the zone would be if the unit were running.
- Cooling comfort range: desired range while cooling (target +/- cooling offsets).
- Heating comfort range: desired range while heating (target +/- heating offsets).
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from . import SmarterZonesConfigEntry
from .const import CONF_HUMIDITY_SENSOR, CONF_LOCAL_SENSOR, CONF_ZONES
from .entity import SmarterZonesZoneEntity, hub_device_info

PROJECTED_OPTIONS = ["Open", "Closed"]
UNREADABLE = ("unknown", "unavailable", "none", "")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmarterZonesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager = entry.runtime_data
    zones = {**entry.data, **entry.options}.get(CONF_ZONES, [])
    entities: list = []
    for zone in zones:
        entities.append(ProjectedStatusSensor(manager, entry.entry_id, zone))
        entities.append(CoolingRangeSensor(manager, entry.entry_id, zone))
        entities.append(HeatingRangeSensor(manager, entry.entry_id, zone))
        if zone.get(CONF_LOCAL_SENSOR):
            entities.append(CurrentTemperatureSensor(manager, entry.entry_id, zone))
        if zone.get(CONF_HUMIDITY_SENSOR):
            entities.append(CurrentHumiditySensor(manager, entry.entry_id, zone))
    # Hub-level sensor explaining the auto-fan-speed decision. Created whenever
    # the fan feature could be in play (a speed list is defined, or auto fan
    # speed is enabled in config).
    if manager.fan_speed_modes_defined or manager.auto_fan_speed:
        entities.append(FanDecisionSensor(manager, entry.entry_id, entry.title))
    # Hub-level sensor explaining the auto target-temperature decision (always
    # available, like the fan-decision sensor).
    entities.append(SetpointDecisionSensor(manager, entry.entry_id, entry.title))
    async_add_entities(entities)


class _MirrorSensor(SmarterZonesZoneEntity, SensorEntity):
    """Mirrors the live value of a source sensor onto the zone device.

    This makes the room's current temperature/humidity part of the zone so the
    dashboard card (and the device page) can show it without extra setup.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _source_conf_key: str = ""

    def __init__(self, manager, entry_id: str, zone: dict, key: str) -> None:
        super().__init__(manager, entry_id, zone, key)
        self._source = zone.get(self._source_conf_key)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._sync()
        if self._source:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._source], self._handle_source
                )
            )

    @callback
    def _handle_source(self, event: Event[EventStateChangedData]) -> None:
        self._sync()
        self.async_write_ha_state()

    @callback
    def _sync(self) -> None:
        value = None
        unit = None
        if self._source:
            st = self.hass.states.get(self._source)
            if st is not None and st.state.lower() not in UNREADABLE:
                try:
                    value = float(st.state)
                    unit = st.attributes.get("unit_of_measurement")
                except (ValueError, TypeError):
                    value = None
        self._attr_native_value = value
        if unit:
            self._attr_native_unit_of_measurement = unit


class CurrentTemperatureSensor(_MirrorSensor):
    """The zone's current room temperature (mirror of its local sensor)."""

    _attr_name = "Current temperature"
    _attr_icon = "mdi:thermometer"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "\u00b0C"
    _source_conf_key = CONF_LOCAL_SENSOR

    def __init__(self, manager, entry_id: str, zone: dict) -> None:
        super().__init__(manager, entry_id, zone, "current_temperature")


class CurrentHumiditySensor(_MirrorSensor):
    """The zone's current humidity (mirror of a configured humidity sensor)."""

    _attr_name = "Current humidity"
    _attr_icon = "mdi:water-percent"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _source_conf_key = CONF_HUMIDITY_SENSOR

    def __init__(self, manager, entry_id: str, zone: dict) -> None:
        super().__init__(manager, entry_id, zone, "current_humidity")


class _ZoneStatusSensor(SmarterZonesZoneEntity, SensorEntity):
    """Base for sensors that recompute when the manager re-evaluates a zone."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._recalculate()
        self._manager.register_status_listener(self._zone_id, self._handle_update)
        self.async_on_remove(
            lambda: self._manager.unregister_status_listener(
                self._zone_id, self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self._recalculate()
        self.async_write_ha_state()

    @callback
    def _recalculate(self) -> None:
        raise NotImplementedError


class ProjectedStatusSensor(_ZoneStatusSensor):
    """Open/Closed the zone would be if the unit were running."""

    _attr_name = "Projected status"
    _attr_icon = "mdi:home-thermometer-outline"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PROJECTED_OPTIONS

    def __init__(self, manager, entry_id: str, zone: dict) -> None:
        super().__init__(manager, entry_id, zone, "projected_status")

    @callback
    def _recalculate(self) -> None:
        zone = self._zone
        self._attr_native_value = (
            self._manager.projected_status(zone) if zone is not None else None
        )


class _RangeSensor(_ZoneStatusSensor):
    """Base for the cooling/heating comfort-range sensors."""

    _range_key: str = ""

    @callback
    def _recalculate(self) -> None:
        zone = self._zone
        data = self._manager.comfort_range(zone) if zone is not None else None
        if not data:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        low, high = data[self._range_key]
        unit = self.hass.config.units.temperature_unit
        self._attr_native_value = f"{low:g}\u2013{high:g} {unit}"
        self._attr_extra_state_attributes = {
            "target": data["target"],
            "low": low,
            "high": high,
            "temperature_unit": unit,
        }


class CoolingRangeSensor(_RangeSensor):
    """Desired range while cooling: target - cooling lower .. target + cooling upper."""

    _attr_name = "Cooling comfort range"
    _attr_icon = "mdi:snowflake-thermometer"
    _range_key = "cooling"

    def __init__(self, manager, entry_id: str, zone: dict) -> None:
        super().__init__(manager, entry_id, zone, "cooling_range")


class HeatingRangeSensor(_RangeSensor):
    """Desired range while heating: target - heating lower .. target + heating upper."""

    _attr_name = "Heating comfort range"
    _attr_icon = "mdi:sun-thermometer"
    _range_key = "heating"

    def __init__(self, manager, entry_id: str, zone: dict) -> None:
        super().__init__(manager, entry_id, zone, "heating_range")


class FanDecisionSensor(SensorEntity):
    """Hub sensor that surfaces the auto-fan-speed decision and its reasoning.

    The state is the chosen fan speed (or a status such as Disabled / Idle),
    and the attributes expose the inputs behind it: open zones, worst room
    deviation, the demand fraction, the ordered speed list, and so on.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Fan decision"
    _attr_icon = "mdi:speedometer"

    def __init__(self, manager, entry_id: str, title: str) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry_id}_fan_decision"
        self._attr_device_info = hub_device_info(entry_id, title)
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._recalculate()
        self._manager.register_hub_listener(self._handle_update)
        self.async_on_remove(
            lambda: self._manager.unregister_hub_listener(self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self._recalculate()
        self.async_write_ha_state()

    @callback
    def _recalculate(self) -> None:
        decision = self._manager.fan_decision()
        self._attr_native_value = decision.pop("summary", None)
        self._attr_extra_state_attributes = decision


class SetpointDecisionSensor(SensorEntity):
    """Hub sensor that surfaces the auto target-temperature decision.

    The state is the requested setpoint (or a status such as Disabled / Idle),
    and the attributes expose the inputs behind it: the unit's own reading, the
    open zones and their unmet demand, the demand fraction, the applied bias and
    a plain-language explanation.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Setpoint decision"
    _attr_icon = "mdi:thermostat"

    def __init__(self, manager, entry_id: str, title: str) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry_id}_setpoint_decision"
        self._attr_device_info = hub_device_info(entry_id, title)
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._recalculate()
        self._manager.register_hub_listener(self._handle_update)
        self.async_on_remove(
            lambda: self._manager.unregister_hub_listener(self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self._recalculate()
        self.async_write_ha_state()

    @callback
    def _recalculate(self) -> None:
        decision = self._manager.setpoint_decision()
        self._attr_native_value = decision.pop("summary", None)
        self._attr_extra_state_attributes = decision
