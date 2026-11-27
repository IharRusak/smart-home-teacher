"""Smart Home Teacher integration."""
from __future__ import annotations

from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .custom_actions import async_setup_custom_actions, async_unload_custom_actions

PLATFORMS = [Platform.CONVERSATION]
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


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
    """Set up the conversation agent and previously learned custom phrases."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    try:
        await async_setup_custom_actions(hass, entry)
    except Exception:
        await async_unload_custom_actions(hass, entry)
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    """Unload the agent and unregister its learned custom intent handlers."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await async_unload_custom_actions(hass, entry)
    return unload_ok
