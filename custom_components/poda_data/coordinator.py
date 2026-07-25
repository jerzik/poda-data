"""DataUpdateCoordinator for PODA data."""
from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import NumberStats, PodaAuthError, PodaClient, PodaConnectionError
from .const import CONF_PASSWORD, CONF_USERNAME, DEFAULT_SCAN_INTERVAL_HOURS

_LOGGER = logging.getLogger(__name__)


class PodaDataCoordinator(DataUpdateCoordinator[dict[str, NumberStats]]):
    """Fetches and caches billing data from klient.poda.cz."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        scan_hours = entry.options.get("scan_interval_hours", DEFAULT_SCAN_INTERVAL_HOURS)
        super().__init__(
            hass,
            _LOGGER,
            name="PODA data",
            update_interval=timedelta(hours=scan_hours),
        )
        # Dedicated session/cookie-jar so login state isn't shared with
        # other integrations using HA's default aiohttp session.
        self._session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
        self._client = PodaClient(
            self._session,
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
        )

    async def async_close(self) -> None:
        await self._session.close()

    async def _async_update_data(self) -> dict[str, NumberStats]:
        try:
            return await self._client.async_get_stats()
        except PodaAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except PodaConnectionError as err:
            raise UpdateFailed(f"Error communicating with klient.poda.cz: {err}") from err
