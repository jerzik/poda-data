"""Config flow for PODA data."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .api import PodaAuthError, PodaClient, PodaConnectionError
from .const import CONF_PASSWORD, CONF_USERNAME, DEFAULT_SCAN_INTERVAL_HOURS, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_login(hass: HomeAssistant, username: str, password: str) -> None:
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
    try:
        client = PodaClient(session, username, password)
        await client.async_login()
    finally:
        await session.close()


class PodaDataConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for PODA data."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME])
            self._abort_if_unique_id_configured()

            try:
                await _validate_login(
                    self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except PodaAuthError:
                errors["base"] = "invalid_auth"
            except PodaConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating PODA login")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"PODA ({user_input[CONF_USERNAME]})", data=user_input
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "PodaDataOptionsFlow":
        return PodaDataOptionsFlow(config_entry)


class PodaDataOptionsFlow(config_entries.OptionsFlow):
    """Options flow allowing the scan interval to be adjusted."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            "scan_interval_hours", DEFAULT_SCAN_INTERVAL_HOURS
        )
        schema = vol.Schema(
            {
                vol.Required("scan_interval_hours", default=current): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=24)
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
