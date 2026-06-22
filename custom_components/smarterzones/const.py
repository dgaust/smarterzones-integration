"""Constants for the SmarterZones integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "smarterzones"
PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]

MANUFACTURER = "Smarter Zones"
HUB_MODEL = "Zone controller"
ZONE_MODEL = "Zone"

# Global config keys
CONF_CLIMATE_DEVICE = "climate_device"
CONF_EXTERIOR_SENSOR = "exterior_sensor"
CONF_COMMON_ZONE_SWITCH = "common_zone_switch"
CONF_FORCE_AUTO_FAN = "force_auto_fan"
CONF_FAN_AUTO_SUFFIX = "fan_auto_suffix"
CONF_AUTO_FAN_SPEED = "auto_fan_speed"
CONF_FAN_SPEED_MODES = "fan_speed_modes"
CONF_FAN_FULL_DEVIATION = "fan_full_deviation"
CONF_AUTO_SETPOINT = "auto_setpoint"
CONF_SETPOINT_MAX_BIAS = "setpoint_max_bias"
CONF_EXPOSE_CLIMATE = "expose_climate"
CONF_AUTO_POWER_ON = "auto_power_on"
CONF_TRIGGER_SENSOR = "trigger_sensor"
CONF_TRIGGER_HIGH = "trigger_high"
CONF_TRIGGER_LOW = "trigger_low"

# Zone keys
CONF_ZONES = "zones"
CONF_ZONE_ID = "id"
CONF_ZONE_NAME = "name"
CONF_ZONE_SWITCH = "zone_switch"
CONF_LOCAL_SENSOR = "local_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_TARGET_INIT = "target_init"
CONF_COOL_UPPER = "cool_upper"
CONF_COOL_LOWER = "cool_lower"
CONF_HEAT_UPPER = "heat_upper"
CONF_HEAT_LOWER = "heat_lower"
CONF_CONDITIONS = "conditions"
CONF_CONDITION_ENTITY = "entity"
CONF_CONDITION_STATE = "state"

# Defaults
DEFAULT_OFFSET = 0.3
DEFAULT_FAN_AUTO_SUFFIX = "/Auto"
DEFAULT_TRIGGER_HIGH = 31.0
DEFAULT_TRIGGER_LOW = 17.0
DEFAULT_TARGET_TEMP = 22.0

# Auto fan-speed tuning.
# Default deviation (in degrees) of the worst open zone that maps to full fan
# speed. Overridable per-hub via CONF_FAN_FULL_DEVIATION.
FAN_FULL_DEVIATION = 4.0
# Extra demand each additional open zone adds (as a fraction of full), capped.
FAN_PER_ZONE_BUMP = 0.1
FAN_MAX_ZONE_BUMP = 0.3

# Auto target-temperature (setpoint) tuning.
# The unit's own current_temperature sensor (return air) often disagrees with
# the actual rooms, so it can stop conditioning before rooms are comfortable.
# When enabled, SmarterZones biases the unit's setpoint relative to that same
# reading - the value the unit compares against - using the real room sensors
# to decide direction and magnitude (mirrors the fan-speed demand model).
#
# Worst unmet room demand (in degrees) that maps to the full bias.
SETPOINT_FULL_DEVIATION = 4.0
# Default maximum degrees to push the setpoint past the unit's reading at full
# demand (live, overridable via the hub 'Setpoint bias' number entity).
SETPOINT_MAX_BIAS = 2.0
# Floor bias applied whenever any open zone still needs conditioning, so the
# unit reliably keeps running even on small demand.
SETPOINT_MIN_BIAS = 0.5
# Once every open zone is satisfied, ease the setpoint this far the other way so
# the unit can idle instead of over-conditioning.
SETPOINT_REST_MARGIN = 1.0
# Extra demand each additional open zone adds (fraction of full), capped.
SETPOINT_PER_ZONE_BUMP = 0.1
SETPOINT_MAX_ZONE_BUMP = 0.3
# Don't rewrite the setpoint for changes smaller than this (avoids churn).
SETPOINT_DEADBAND = 0.3

# Bounds for the hub 'Setpoint bias' number entity.
SETPOINT_BIAS_MIN = 0.5
SETPOINT_BIAS_MAX = 6.0
SETPOINT_BIAS_STEP = 0.5

# Switch command retry. Some zone controllers (e.g. Daikin AirBase) drop
# commands when many arrive at once, so each switch action is retried.
SWITCH_RETRY_ATTEMPTS = 3
SWITCH_RETRY_DELAY = 0.4  # seconds between attempts

# Bounds for the per-zone target-temperature number entity.
TARGET_TEMP_MIN = 16.0
TARGET_TEMP_MAX = 32.0
TARGET_TEMP_STEP = 0.5

# States that mean a sensor has no usable numeric value.
UNAVAILABLE_STATES = {"unknown", "unavailable", "none", ""}
