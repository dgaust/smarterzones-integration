"""Switches: per-zone smart-control and manual open/close, plus hub-level
auto-fan-speed and auto target-temperature toggles."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from . import SmarterZonesConfigEntry
from .const import CONF_ZONE_SWITCH, CONF_ZONES
from .entity import SmarterZonesZoneEntity, hub_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmarterZonesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager = entry.runtime_data
    zones = {**entry.data, **entry.options}.get(CONF_ZONES, [])
    entities: list[Entity] = []
    for zone in zones:
        entities.append(SmartControlSwitch(manager, entry.entry_id, zone))
        entities.append(OpenZoneSwitch(manager, entry.entry_id, zone))
    # Only offer the runtime auto-fan-speed toggle when the user has defined the
    # ordered fan-speed string for it to act on.
    if manager.fan_speed_modes_defined:
        entities.append(AutoFanSpeedSwitch(manager, entry.entry_id, entry.title))
    else:
        _LOGGER.debug(
            "No fan-speed string configured; auto fan speed switch not created"
        )
    # Auto target temperature is always available to enable from the hub device,
    # mirroring the auto-fan-speed switch; the config option only sets the default.
    entities.append(AutoSetpointSwitch(manager, entry.entry_id, entry.title))
    async_add_entities(entities)


class SmartControlSwitch(SmarterZonesZoneEntity, SwitchEntity, RestoreEntity):
    """When on, SmarterZones manages the zone; when off, it's left manual."""

    _attr_name = "Smart control"
    _attr_icon = "mdi:thermostat-auto"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, manager, entry_id: str, zone: dict) -> None:
        super().__init__(manager, entry_id, zone, "smart_control")
        self._attr_is_on = True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"
        self._manager.set_zone_enabled(self._zone_id, self._attr_is_on)
        # Whichever of (number, switch) registers second triggers the first
        # real evaluation; async_manage_zone no-ops until the zone is ready.
        if (zone := self._zone) is not None:
            await self._manager.async_manage_zone(zone)

    async def async_turn_on(self, **kwargs) -> None:
        _LOGGER.info("Smart control enabled for zone '%s'", self._attr_name)
        self._attr_is_on = True
        self._manager.set_zone_enabled(self._zone_id, True)
        self.async_write_ha_state()
        if (zone := self._zone) is not None:
            await self._manager.async_manage_zone(zone)

    async def async_turn_off(self, **kwargs) -> None:
        _LOGGER.info(
            "Smart control disabled for zone '%s' (now manual)", self._attr_name
        )
        self._attr_is_on = False
        self._manager.set_zone_enabled(self._zone_id, False)
        self.async_write_ha_state()


class OpenZoneSwitch(SmarterZonesZoneEntity, SwitchEntity):
    """Manual open/close control for a zone's damper.

    Mirrors and drives the underlying zone switch directly, so a zone can be
    opened or closed by hand - useful when smart control is off. While smart
    control is on the manager still owns the zone and will re-evaluate it on the
    next sensor/climate change, so a manual change made then is temporary.
    """

    _attr_name = "Open zone"
    _attr_icon = "mdi:valve"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, manager, entry_id: str, zone: dict) -> None:
        super().__init__(manager, entry_id, zone, "open_zone")
        self._damper = zone[CONF_ZONE_SWITCH]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._sync()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._damper], self._handle_source
            )
        )

    @callback
    def _handle_source(self, event) -> None:
        self._sync()
        self.async_write_ha_state()

    @callback
    def _sync(self) -> None:
        state = self.hass.states.get(self._damper)
        self._attr_is_on = state is not None and state.state == "on"
        self._attr_available = state is not None

    async def async_turn_on(self, **kwargs) -> None:
        _LOGGER.info("Manual open requested for zone '%s'", self._zone_id)
        await self._manager.async_set_zone_damper(self._damper, True)

    async def async_turn_off(self, **kwargs) -> None:
        _LOGGER.info("Manual close requested for zone '%s'", self._zone_id)
        await self._manager.async_set_zone_damper(self._damper, False)


class AutoFanSpeedSwitch(SwitchEntity, RestoreEntity):
    """Hub-level toggle for the automatic fan-speed feature.

    Only created when an ordered fan-speed string is configured. Restores its
    last state across restarts, falling back to the configured default.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Auto fan speed"
    _attr_icon = "mdi:fan-auto"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, manager, entry_id: str, title: str) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry_id}_auto_fan_speed"
        self._attr_device_info = hub_device_info(entry_id, title)
        self._attr_is_on = manager.auto_fan_speed

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"
        # Register the resolved state; the manager's own setup pass applies it.
        self._manager.set_auto_fan_speed(self._attr_is_on)

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self._manager.set_auto_fan_speed(True)
        self.async_write_ha_state()
        await self._manager.async_manage_all()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self._manager.set_auto_fan_speed(False)
        self.async_write_ha_state()


class AutoSetpointSwitch(SwitchEntity, RestoreEntity):
    """Hub-level toggle for the automatic target-temperature feature.

    Always created; the config option only sets the default. Restores its last
    state across restarts. When switched on it remembers the unit's current
    target temperature (the user's "base" level) and re-instates it when switched
    off, so disabling the feature returns the unit to where the user left it. That
    base is published as the ``base_temperature`` attribute so it survives a
    restart while the feature is on.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Auto target temperature"
    _attr_icon = "mdi:thermostat-auto"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, manager, entry_id: str, title: str) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry_id}_auto_setpoint"
        self._attr_device_info = hub_device_info(entry_id, title)
        self._attr_is_on = manager.auto_setpoint

    @property
    def extra_state_attributes(self) -> dict:
        base = self._manager.setpoint_base
        return {} if base is None else {"base_temperature": base}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        restored_base = None
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"
            restored_base = last.attributes.get("base_temperature")
        # While on, re-establish the remembered base: use the persisted value if we
        # have one (a restart), otherwise capture the unit's current setpoint now
        # (first enable via the config default, before any bias is applied).
        if self._attr_is_on:
            if restored_base is not None:
                self._manager.set_setpoint_base(restored_base)
            else:
                self._manager.capture_setpoint_base()
        # Register the resolved state; the manager's own setup pass applies it.
        self._manager.set_auto_setpoint(self._attr_is_on)

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self._manager.set_auto_setpoint(True)
        # Remember the user's current setpoint before the first bias is applied.
        self._manager.capture_setpoint_base()
        self.async_write_ha_state()
        await self._manager.async_manage_all()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self._manager.set_auto_setpoint(False)
        # Put the unit's setpoint back to the remembered base level.
        await self._manager.async_restore_setpoint_base()
        self.async_write_ha_state()
