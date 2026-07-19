"""The SmarterZones integration."""

from __future__ import annotations

import logging
import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry

from .const import CARD_VERSION, CONF_ZONE_ID, CONF_ZONES, DOMAIN, PLATFORMS
from .coordinator import SmarterZonesManager
from .entity import hub_device_info

_LOGGER = logging.getLogger(__name__)

type SmarterZonesConfigEntry = ConfigEntry[SmarterZonesManager]

_CARD_URL = "/smarterzones/smarterzones-zone-card.js"
_CARD_REGISTERED = f"{DOMAIN}_card_registered"


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the Lovelace card and auto-load it on the frontend.

    Best-effort: any failure here just means the user adds the resource
    manually, so it must never block integration setup.

    The file is served *with* cache headers and auto-loaded with a
    ``?v=<CARD_VERSION>`` query string. Caching matters for reliability, not
    just speed: an uncached module is re-fetched on every page load, and if it
    hasn't defined its custom element by the time Lovelace renders the view, the
    card shows "Configuration error" instead. The version string busts that
    cache whenever the card actually changes.
    """
    if hass.data.get(_CARD_REGISTERED):
        return
    card_path = os.path.join(os.path.dirname(__file__), "www", "smarterzones-zone-card.js")
    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(_CARD_URL, card_path, True)]
        )
    except Exception:  # noqa: BLE001 - fall back to the legacy sync API
        try:
            hass.http.register_static_path(_CARD_URL, card_path, True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Could not serve the SmarterZones card (%s); add %s as a "
                "Lovelace resource manually",
                err,
                card_path,
            )
            return
    # Only mark as done once the file is actually being served, so a failed
    # attempt can be retried by a later entry setup or a reload.
    hass.data[_CARD_REGISTERED] = True
    versioned_url = f"{_CARD_URL}?v={CARD_VERSION}"
    try:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, versioned_url)
        _LOGGER.debug("Registered SmarterZones Lovelace card at %s", versioned_url)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Could not auto-load the SmarterZones card (%s); add the resource "
            "%s (JavaScript module) under Settings > Dashboards > Resources",
            err,
            versioned_url,
        )


async def async_setup_entry(
    hass: HomeAssistant, entry: SmarterZonesConfigEntry
) -> bool:
    """Set up SmarterZones from a config entry."""
    _LOGGER.info("Setting up SmarterZones config entry '%s'", entry.title)
    await _async_register_card(hass)
    manager = SmarterZonesManager(hass, entry)
    entry.runtime_data = manager

    # Create the top-level controller device up front so zone devices can link
    # to it via `via_device`, even before any entity is added.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        **hub_device_info(entry.entry_id, entry.title),
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Listeners come up after the platforms so the per-zone number/switch
    # entities have already registered their values with the manager.
    await manager.async_setup()

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _LOGGER.debug("SmarterZones entry '%s' setup complete", entry.title)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: SmarterZonesConfigEntry
) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading SmarterZones config entry '%s'", entry.title)
    if (manager := entry.runtime_data) is not None:
        manager.async_unload()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(
    hass: HomeAssistant, entry: SmarterZonesConfigEntry
) -> None:
    """Reload the entry when options change (zones added/removed/edited)."""
    _LOGGER.info(
        "Options changed for '%s'; reloading SmarterZones", entry.title
    )
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SmarterZonesConfigEntry, device: DeviceEntry
) -> bool:
    """Allow deleting a zone device only once that zone no longer exists."""
    zones = {**entry.data, **entry.options}.get(CONF_ZONES, [])
    active = {entry.entry_id} | {
        f"{entry.entry_id}_{zone[CONF_ZONE_ID]}" for zone in zones
    }
    own_ids = {ident for domain, ident in device.identifiers if domain == DOMAIN}
    allowed = own_ids.isdisjoint(active)
    _LOGGER.debug(
        "Device removal request for %s: %s",
        device.name or own_ids,
        "allowed (zone no longer configured)" if allowed else "blocked (still active)",
    )
    return allowed
