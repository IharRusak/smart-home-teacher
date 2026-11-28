"""Smart Home Teacher integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_CLEANUP_LEARNING_RESULT
from .custom_actions import (
    async_cleanup_learning_result,
    async_setup_custom_actions,
    async_unload_custom_actions,
)

PLATFORMS = [Platform.CONVERSATION]
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
CLEANUP_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("request_id"): cv.string,
        vol.Required("result_type"): vol.In(
            {"custom_phrase", "entity_alias", "area_alias"}
        ),
        vol.Required("target_id"): cv.string,
        vol.Required("value"): cv.string,
        vol.Required("language"): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Schedule the imported config entry without blocking integration setup."""
    if not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={},
            ),
            "smart_home_teacher import config flow",
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    """Set up the conversation agent and load saved phrase mappings."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    try:
        await async_setup_custom_actions(hass, entry)
    except Exception:
        await async_unload_custom_actions(hass, entry)
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        raise

    async def async_cleanup_result(call: ServiceCall) -> None:
        """Remove the exact alias or custom intent named by the automation."""
        await async_cleanup_learning_result(
            hass,
            call.data,
            call.data["request_id"],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEANUP_LEARNING_RESULT,
        async_cleanup_result,
        schema=CLEANUP_SERVICE_SCHEMA,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    """Unload the learning agent while keeping native HA intents active."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await async_unload_custom_actions(hass, entry)
        hass.services.async_remove(DOMAIN, SERVICE_CLEANUP_LEARNING_RESULT)
    return unload_ok
