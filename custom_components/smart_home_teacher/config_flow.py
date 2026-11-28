"""Config flow for Smart Home Teacher."""
from __future__ import annotations

from homeassistant import config_entries

from .const import DOMAIN


def _display_name(language: str) -> str:
    """Return the project name in the Home Assistant UI language."""
    return "Учитель умного дома" if language.replace("_", "-").lower().split("-")[0] == "ru" else "Smart Home Teacher"


class SmartHomeTeacherConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create the single Smart Home Teacher config entry."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        """Create the entry from the UI."""
        return await self._async_create_entry()

    async def async_step_import(self, user_input: dict | None = None):
        """Create the entry from YAML configuration."""
        return await self._async_create_entry()

    async def _async_create_entry(self):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        title = _display_name(self.hass.config.language)
        return self.async_create_entry(title=title, data={})
