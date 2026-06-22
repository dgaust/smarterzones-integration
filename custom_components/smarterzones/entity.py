"""Shared entity base and device helpers for SmarterZones."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import (
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    DOMAIN,
    HUB_MODEL,
    MANUFACTURER,
    ZONE_MODEL,
)


def hub_device_info(entry_id: str, title: str) -> DeviceInfo:
    """Device info for the top-level controller device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=title,
        manufacturer=MANUFACTURER,
        model=HUB_MODEL,
        entry_type=None,
    )


def zone_device_info(entry_id: str, zone: dict) -> DeviceInfo:
    """Device info for a single zone, linked under the controller device."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{zone[CONF_ZONE_ID]}")},
        name=zone[CONF_ZONE_NAME],
        manufacturer=MANUFACTURER,
        model=ZONE_MODEL,
        via_device=(DOMAIN, entry_id),
    )


class SmarterZonesZoneEntity(Entity):
    """Base class for entities that belong to a zone sub-device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager, entry_id: str, zone: dict, key: str) -> None:
        self._manager = manager
        self._entry_id = entry_id
        self._zone_id = zone[CONF_ZONE_ID]
        self._attr_unique_id = f"{entry_id}_{self._zone_id}_{key}"
        self._attr_device_info = zone_device_info(entry_id, zone)

    @property
    def _zone(self) -> dict | None:
        """Return the live zone config (may be None if it was removed)."""
        return self._manager.get_zone(self._zone_id)
