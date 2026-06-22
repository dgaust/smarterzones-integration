"""Binary sensors for SmarterZones: zone-open mirror and conditions-met."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from . import SmarterZonesConfigEntry
from .const import (
    CONF_CONDITION_ENTITY,
    CONF_CONDITION_STATE,
    CONF_CONDITIONS,
    CONF_ZONE_SWITCH,
    CONF_ZONES,
)
from .entity import SmarterZonesZoneEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmarterZonesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager = entry.runtime_data
    zones = {**entry.data, **entry.options}.get(CONF_ZONES, [])
    entities: list = []
    for zone in zones:
        entities.append(ZoneOpenBinarySensor(manager, entry.entry_id, zone))
        entities.append(ConditionsMetBinarySensor(manager, entry.entry_id, zone))
    async_add_entities(entities)


class ZoneOpenBinarySensor(SmarterZonesZoneEntity, BinarySensorEntity):
    """On (Open) when the zone's switch is on - air is flowing to the zone."""

    _attr_name = "Zone open"
    _attr_device_class = BinarySensorDeviceClass.OPENING

    def __init__(self, manager, entry_id: str, zone: dict) -> None:
        super().__init__(manager, entry_id, zone, "zone_open")
        self._source = zone[CONF_ZONE_SWITCH]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._update()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._source], self._handle_source
            )
        )

    @callback
    def _handle_source(self, event) -> None:
        self._update()
        self.async_write_ha_state()

    @callback
    def _update(self) -> None:
        state = self.hass.states.get(self._source)
        self._attr_is_on = state is not None and state.state == "on"
        self._attr_available = state is not None


class ConditionsMetBinarySensor(SmarterZonesZoneEntity, BinarySensorEntity):
    """On when all of the zone's conditions are satisfied (always on if none)."""

    _attr_name = "Conditions met"
    _attr_icon = "mdi:check-decagram"

    def __init__(self, manager, entry_id: str, zone: dict) -> None:
        super().__init__(manager, entry_id, zone, "conditions_met")
        self._conditions = list(zone.get(CONF_CONDITIONS, []))
        self._sources = [c[CONF_CONDITION_ENTITY] for c in self._conditions]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._update()
        if self._sources:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, self._sources, self._handle_source
                )
            )

    @callback
    def _handle_source(self, event) -> None:
        self._update()
        self.async_write_ha_state()

    @callback
    def _update(self) -> None:
        met = True
        detail = []
        for cond in self._conditions:
            entity = cond[CONF_CONDITION_ENTITY]
            required = str(cond[CONF_CONDITION_STATE])
            state = self.hass.states.get(entity)
            current = state.state if state else "unknown"
            ok = current.lower() == required.lower()
            met = met and ok
            detail.append(
                {"entity": entity, "required": required, "current": current, "ok": ok}
            )
        self._attr_is_on = met
        self._attr_extra_state_attributes = {"conditions": detail}
