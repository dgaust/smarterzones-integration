"""Config and options flow for SmarterZones."""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import SmarterZonesConfigEntry
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
    CONF_EXPOSE_CLIMATE,
    CONF_EXTERIOR_SENSOR,
    CONF_FAN_AUTO_SUFFIX,
    CONF_FAN_SPEED_MODES,
    CONF_FORCE_AUTO_FAN,
    CONF_HEAT_LOWER,
    CONF_HEAT_UPPER,
    CONF_HUMIDITY_SENSOR,
    CONF_LOCAL_SENSOR,
    CONF_TARGET_INIT,
    CONF_TRIGGER_HIGH,
    CONF_TRIGGER_LOW,
    CONF_TRIGGER_SENSOR,
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    CONF_ZONE_SWITCH,
    CONF_ZONES,
    DEFAULT_FAN_AUTO_SUFFIX,
    DEFAULT_OFFSET,
    DEFAULT_TARGET_TEMP,
    DEFAULT_TRIGGER_HIGH,
    DEFAULT_TRIGGER_LOW,
    DOMAIN,
    TARGET_TEMP_MAX,
    TARGET_TEMP_MIN,
    TARGET_TEMP_STEP,
)

_OFFSET_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(min=0, max=10, step=0.1, mode="box")
)


def _settings_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_EXTERIOR_SENSOR,
                default=defaults.get(CONF_EXTERIOR_SENSOR, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(
                CONF_COMMON_ZONE_SWITCH,
                default=defaults.get(CONF_COMMON_ZONE_SWITCH, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="switch")
            ),
            vol.Optional(
                CONF_EXPOSE_CLIMATE,
                default=defaults.get(CONF_EXPOSE_CLIMATE, True),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_FORCE_AUTO_FAN,
                default=defaults.get(CONF_FORCE_AUTO_FAN, False),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_FAN_AUTO_SUFFIX,
                default=defaults.get(CONF_FAN_AUTO_SUFFIX, DEFAULT_FAN_AUTO_SUFFIX),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_AUTO_FAN_SPEED,
                default=defaults.get(CONF_AUTO_FAN_SPEED, False),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_FAN_SPEED_MODES,
                default=defaults.get(CONF_FAN_SPEED_MODES, vol.UNDEFINED),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_AUTO_SETPOINT,
                default=defaults.get(CONF_AUTO_SETPOINT, False),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_AUTO_POWER_ON,
                default=defaults.get(CONF_AUTO_POWER_ON, False),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_TRIGGER_SENSOR,
                default=defaults.get(CONF_TRIGGER_SENSOR, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(
                CONF_TRIGGER_HIGH,
                default=defaults.get(CONF_TRIGGER_HIGH, DEFAULT_TRIGGER_HIGH),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=50, step=0.5, mode="box")
            ),
            vol.Optional(
                CONF_TRIGGER_LOW,
                default=defaults.get(CONF_TRIGGER_LOW, DEFAULT_TRIGGER_LOW),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=50, step=0.5, mode="box")
            ),
        }
    )


def _settings_keys() -> list[str]:
    """CONF_* keys that live in the global settings schema (for clearing on save)."""
    return [marker.schema for marker in _settings_schema({}).schema]


def _zone_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_ZONE_NAME): selector.TextSelector(),
            vol.Required(CONF_ZONE_SWITCH): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="switch")
            ),
            vol.Required(CONF_LOCAL_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            vol.Optional(CONF_HUMIDITY_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="humidity")
            ),
            vol.Optional(
                CONF_TARGET_INIT, default=DEFAULT_TARGET_TEMP
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=TARGET_TEMP_MIN,
                    max=TARGET_TEMP_MAX,
                    step=TARGET_TEMP_STEP,
                    mode="box",
                    unit_of_measurement="°C",
                )
            ),
            vol.Optional(CONF_COOL_UPPER, default=DEFAULT_OFFSET): _OFFSET_SELECTOR,
            vol.Optional(CONF_COOL_LOWER, default=DEFAULT_OFFSET): _OFFSET_SELECTOR,
            vol.Optional(CONF_HEAT_UPPER, default=DEFAULT_OFFSET): _OFFSET_SELECTOR,
            vol.Optional(CONF_HEAT_LOWER, default=DEFAULT_OFFSET): _OFFSET_SELECTOR,
            vol.Optional("add_conditions", default=False): selector.BooleanSelector(),
        }
    )


def _condition_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CONDITION_ENTITY): selector.EntitySelector(),
            vol.Required(CONF_CONDITION_STATE, default="off"): selector.TextSelector(),
            vol.Optional("add_another", default=False): selector.BooleanSelector(),
        }
    )


def _edit_zone_schema(zone: dict[str, Any]) -> vol.Schema:
    """Structural fields of a zone, pre-filled for editing.

    Target temperature and offsets are deliberately omitted: those are live,
    restored controls on the zone device, so editing them here would be
    overwritten by their saved values on reload.
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_ZONE_NAME, default=zone[CONF_ZONE_NAME]
            ): selector.TextSelector(),
            vol.Required(
                CONF_ZONE_SWITCH, default=zone[CONF_ZONE_SWITCH]
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="switch")
            ),
            vol.Required(
                CONF_LOCAL_SENSOR, default=zone[CONF_LOCAL_SENSOR]
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor", device_class="temperature"
                )
            ),
            vol.Optional(
                CONF_HUMIDITY_SENSOR,
                default=zone.get(CONF_HUMIDITY_SENSOR, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="humidity")
            ),
            vol.Optional(
                "redefine_conditions", default=False
            ): selector.BooleanSelector(),
        }
    )


def _build_zone(
    user_input: dict[str, Any],
    zone_id: str | None = None,
    conditions: list | None = None,
) -> dict[str, Any]:
    return {
        CONF_ZONE_ID: zone_id or uuid.uuid4().hex[:8],
        CONF_ZONE_NAME: user_input[CONF_ZONE_NAME],
        CONF_ZONE_SWITCH: user_input[CONF_ZONE_SWITCH],
        CONF_LOCAL_SENSOR: user_input[CONF_LOCAL_SENSOR],
        CONF_HUMIDITY_SENSOR: user_input.get(CONF_HUMIDITY_SENSOR),
        CONF_TARGET_INIT: user_input[CONF_TARGET_INIT],
        CONF_COOL_UPPER: user_input[CONF_COOL_UPPER],
        CONF_COOL_LOWER: user_input[CONF_COOL_LOWER],
        CONF_HEAT_UPPER: user_input[CONF_HEAT_UPPER],
        CONF_HEAT_LOWER: user_input[CONF_HEAT_LOWER],
        CONF_CONDITIONS: conditions if conditions is not None else [],
    }


def _apply_zone_edit(
    existing: dict[str, Any],
    user_input: dict[str, Any],
    conditions: list | None = None,
) -> dict[str, Any]:
    """Return a copy of an existing zone with edited structural fields.

    Preserves the zone id, target and offsets; replaces conditions only when a
    new list is supplied.
    """
    updated = dict(existing)
    updated[CONF_ZONE_NAME] = user_input[CONF_ZONE_NAME]
    updated[CONF_ZONE_SWITCH] = user_input[CONF_ZONE_SWITCH]
    updated[CONF_LOCAL_SENSOR] = user_input[CONF_LOCAL_SENSOR]
    updated[CONF_HUMIDITY_SENSOR] = user_input.get(CONF_HUMIDITY_SENSOR)
    if conditions is not None:
        updated[CONF_CONDITIONS] = conditions
    return updated


def _add_condition(zone: dict[str, Any], user_input: dict[str, Any]) -> None:
    zone[CONF_CONDITIONS].append(
        {
            CONF_CONDITION_ENTITY: user_input[CONF_CONDITION_ENTITY],
            CONF_CONDITION_STATE: user_input[CONF_CONDITION_STATE],
        }
    )


class SmarterZonesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: controller + global settings, then add zones."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._zones: list[dict[str, Any]] = []
        self._current_zone: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            climate_device = user_input.pop(CONF_CLIMATE_DEVICE)
            await self.async_set_unique_id(climate_device)
            self._abort_if_unique_id_configured()
            self._data = {CONF_CLIMATE_DEVICE: climate_device}
            self._options = dict(user_input)
            return await self.async_step_zone()

        schema = vol.Schema(
            {
                vol.Required(CONF_CLIMATE_DEVICE): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate")
                ),
            }
        ).extend(_settings_schema({}).schema)
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            add_conditions = user_input.pop("add_conditions", False)
            self._current_zone = _build_zone(user_input)
            if add_conditions:
                return await self.async_step_condition()
            self._zones.append(self._current_zone)
            return await self.async_step_zone_menu()
        return self.async_show_form(step_id="zone", data_schema=_zone_schema())

    async def async_step_condition(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            add_another = user_input.pop("add_another", False)
            assert self._current_zone is not None
            _add_condition(self._current_zone, user_input)
            if add_another:
                return await self.async_step_condition()
            self._zones.append(self._current_zone)
            return await self.async_step_zone_menu()
        return self.async_show_form(
            step_id="condition", data_schema=_condition_schema()
        )

    async def async_step_zone_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="zone_menu", menu_options=["zone", "finish"]
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._options[CONF_ZONES] = self._zones
        title = self._data[CONF_CLIMATE_DEVICE].split(".")[-1].replace("_", " ").title()
        return self.async_create_entry(
            title=f"Smarter Zones ({title})",
            data=self._data,
            options=self._options,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: SmarterZonesConfigEntry,
    ) -> SmarterZonesOptionsFlow:
        return SmarterZonesOptionsFlow()


class SmarterZonesOptionsFlow(OptionsFlow):
    """Edit settings, add zones, or remove zones after setup."""

    def __init__(self) -> None:
        self._current_zone: dict[str, Any] | None = None
        self._edit_zone_id: str | None = None
        self._new_conditions: list[dict[str, Any]] = []

    @property
    def _options(self) -> dict[str, Any]:
        return dict(self.config_entry.options)

    @property
    def _zones(self) -> list[dict[str, Any]]:
        return list(self.config_entry.options.get(CONF_ZONES, []))

    def _save(self, options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "add_zone", "edit_zone", "remove_zone"],
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            options = self._options
            # Drop any optional settings the user cleared (a blank entity selector,
            # e.g. removing the common zone, is simply absent from user_input), then
            # write back what they submitted. Non-settings keys (CONF_ZONES) are kept.
            for key in _settings_keys():
                options.pop(key, None)
            options.update(user_input)
            return self._save(options)
        return self.async_show_form(
            step_id="settings", data_schema=_settings_schema(self._options)
        )

    async def async_step_add_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            add_conditions = user_input.pop("add_conditions", False)
            self._current_zone = _build_zone(user_input)
            if add_conditions:
                return await self.async_step_add_condition()
            return self._append_zone()
        return self.async_show_form(step_id="add_zone", data_schema=_zone_schema())

    async def async_step_add_condition(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            add_another = user_input.pop("add_another", False)
            assert self._current_zone is not None
            _add_condition(self._current_zone, user_input)
            if add_another:
                return await self.async_step_add_condition()
            return self._append_zone()
        return self.async_show_form(
            step_id="add_condition", data_schema=_condition_schema()
        )

    def _append_zone(self) -> ConfigFlowResult:
        assert self._current_zone is not None
        options = self._options
        zones = self._zones
        zones.append(self._current_zone)
        options[CONF_ZONES] = zones
        return self._save(options)

    async def async_step_edit_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zones = self._zones
        if not zones:
            return self.async_abort(reason="no_zones")
        if user_input is not None:
            self._edit_zone_id = user_input["zone"]
            return await self.async_step_edit_zone_details()
        choices = [
            selector.SelectOptionDict(value=z[CONF_ZONE_ID], label=z[CONF_ZONE_NAME])
            for z in zones
        ]
        schema = vol.Schema(
            {
                vol.Required("zone"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=choices)
                )
            }
        )
        return self.async_show_form(step_id="edit_zone", data_schema=schema)

    async def async_step_edit_zone_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zone = next(
            (z for z in self._zones if z[CONF_ZONE_ID] == self._edit_zone_id), None
        )
        if zone is None:
            return self.async_abort(reason="no_zones")
        if user_input is not None:
            redefine = user_input.pop("redefine_conditions", False)
            # Keep existing conditions unless the user chose to redefine them.
            self._current_zone = _apply_zone_edit(zone, user_input)
            if redefine:
                self._new_conditions = []
                return await self.async_step_edit_condition()
            return self._replace_zone(self._current_zone)
        existing = len(zone.get(CONF_CONDITIONS, []))
        return self.async_show_form(
            step_id="edit_zone_details",
            data_schema=_edit_zone_schema(zone),
            description_placeholders={
                "name": zone[CONF_ZONE_NAME],
                "conditions": str(existing),
            },
        )

    async def async_step_edit_condition(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            add_another = user_input.pop("add_another", False)
            _add_condition({CONF_CONDITIONS: self._new_conditions}, user_input)
            if add_another:
                return await self.async_step_edit_condition()
            assert self._current_zone is not None
            updated = dict(self._current_zone)
            updated[CONF_CONDITIONS] = self._new_conditions
            return self._replace_zone(updated)
        return self.async_show_form(
            step_id="edit_condition", data_schema=_condition_schema()
        )

    def _replace_zone(self, updated_zone: dict[str, Any]) -> ConfigFlowResult:
        options = self._options
        options[CONF_ZONES] = [
            updated_zone if z[CONF_ZONE_ID] == updated_zone[CONF_ZONE_ID] else z
            for z in self._zones
        ]
        return self._save(options)

    async def async_step_remove_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zones = self._zones
        if not zones:
            return self.async_abort(reason="no_zones")
        if user_input is not None:
            keep = [z for z in zones if z[CONF_ZONE_ID] != user_input["zone"]]
            options = self._options
            options[CONF_ZONES] = keep
            return self._save(options)
        choices = [
            selector.SelectOptionDict(value=z[CONF_ZONE_ID], label=z[CONF_ZONE_NAME])
            for z in zones
        ]
        schema = vol.Schema(
            {
                vol.Required("zone"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=choices)
                )
            }
        )
        return self.async_show_form(step_id="remove_zone", data_schema=schema)
