"""Register the bundled Lovelace card as a dashboard resource.

The card is served by the integration and loaded the way every other dashboard
card is - as a Lovelace resource - rather than through ``add_extra_js_url``.
That matters: extra modules are imported at the very start of the page, before
Home Assistant's frontend has booted, and the frontend then replaces the
custom-element registry (a scoped-registry polyfill) with one that doesn't
know elements defined earlier. On phones the card often won the race and
showed "Custom element doesn't exist" / "Configuration error". Resources are
loaded by Lovelace itself once the frontend is running, which is how HACS
cards (and Home Assistant's own marketplace) load theirs.

This mirrors Home Assistant's marketplace: exactly one storage-mode resource
per card, matched by path, pointing at the current ``?v=<version>`` URL -
created if missing, updated in place on upgrade, duplicates removed, and
deleted when the integration is removed. Dashboards in YAML resource mode
can't be written to, so the user is told which resource to add.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_LOCK_KEY = f"{__name__}_resource_lock"


def _resource_collection(hass: HomeAssistant) -> Any | None:
    """Return Lovelace's storage-mode resource collection, if writable."""
    data = hass.data.get("lovelace")
    if data is None:
        return None
    resources = getattr(data, "resources", None)
    if resources is None and isinstance(data, dict):
        resources = data.get("resources")
    # Only the storage collection has a store; the YAML one is read-only.
    if resources is None or getattr(resources, "store", None) is None:
        return None
    if not all(
        hasattr(resources, attr)
        for attr in ("async_items", "async_create_item", "async_update_item", "async_delete_item")
    ):
        return None
    return resources


async def _async_loaded(resources: Any) -> Any:
    if not getattr(resources, "loaded", True):
        await resources.async_load()
        resources.loaded = True
    return resources


def _ours(item: dict, path: str) -> bool:
    return str(item.get("url", "")).split("?", 1)[0] == path


async def async_register_card_resource(
    hass: HomeAssistant, path: str, version: str
) -> bool:
    """Make sure exactly one dashboard resource loads ``path?v=version``.

    Returns False when the resources can't be written (YAML mode), after
    logging what the user has to add by hand.
    """
    url = f"{path}?v={version}"
    resources = _resource_collection(hass)
    if resources is None:
        if hass.data.get("lovelace") is None:
            _LOGGER.warning(
                "Lovelace isn't loaded, so the card can't be registered. Add "
                "this dashboard resource: url: %s, type: module",
                url,
            )
        else:
            _LOGGER.warning(
                "Dashboard resources are managed in YAML, so the card can't be "
                "registered automatically. Add this resource: url: %s, type: module",
                url,
            )
        return False

    lock = hass.data.setdefault(_LOCK_KEY, asyncio.Lock())
    async with lock:
        await _async_loaded(resources)
        existing = [item for item in resources.async_items() if _ours(item, path)]
        if not existing:
            _LOGGER.info("Adding dashboard resource %s", url)
            await resources.async_create_item({"res_type": "module", "url": url})
            return True

        keep, *duplicates = existing
        if keep.get("url") != url or keep.get("type") != "module":
            _LOGGER.info("Updating dashboard resource %s -> %s", keep.get("url"), url)
            await resources.async_update_item(
                keep["id"], {"res_type": "module", "url": url}
            )
        for item in duplicates:
            _LOGGER.info("Removing duplicate dashboard resource %s", item.get("url"))
            await resources.async_delete_item(item["id"])
    return True


async def async_remove_card_resource(hass: HomeAssistant, path: str) -> None:
    """Remove the card's dashboard resource (the integration is gone)."""
    if (resources := _resource_collection(hass)) is None:
        return
    lock = hass.data.setdefault(_LOCK_KEY, asyncio.Lock())
    async with lock:
        await _async_loaded(resources)
        for item in list(resources.async_items()):
            if _ours(item, path):
                _LOGGER.info("Removing dashboard resource %s", item.get("url"))
                await resources.async_delete_item(item["id"])
