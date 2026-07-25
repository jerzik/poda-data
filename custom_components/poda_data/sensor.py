"""Sensor platform for PODA data."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import NumberStats
from .const import DOMAIN
from .coordinator import PodaDataCoordinator


@dataclass(frozen=True, kw_only=True)
class PodaSensorDescription(SensorEntityDescription):
    """Describes a PODA sensor and how to read its value from NumberStats."""

    value_fn: Callable[[NumberStats], float | int | None] = lambda stats: None


SENSOR_DESCRIPTIONS: tuple[PodaSensorDescription, ...] = (
    PodaSensorDescription(
        key="calls_minutes",
        translation_key="calls_minutes",
        icon="mdi:phone",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.calls_minutes,
    ),
    PodaSensorDescription(
        key="calls_price",
        translation_key="calls_price",
        icon="mdi:phone-outline",
        native_unit_of_measurement="Kč",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: s.calls_price,
    ),
    PodaSensorDescription(
        key="sms_count",
        translation_key="sms_count",
        icon="mdi:message-text",
        native_unit_of_measurement="SMS",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.sms_count,
    ),
    PodaSensorDescription(
        key="sms_price",
        translation_key="sms_price",
        icon="mdi:message-text-outline",
        native_unit_of_measurement="Kč",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: s.sms_price,
    ),
    PodaSensorDescription(
        key="data_used",
        translation_key="data_used",
        icon="mdi:cellphone-arrow-down",
        native_unit_of_measurement="MB",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.data_used_mb,
    ),
    PodaSensorDescription(
        key="data_limit",
        translation_key="data_limit",
        icon="mdi:cellphone-information",
        native_unit_of_measurement="MB",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.data_limit_mb,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up PODA data sensors, creating entities for every discovered number."""
    coordinator: PodaDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    known_numbers: set[str] = set()

    def _add_new_numbers() -> None:
        new_entities: list[PodaSensor] = []
        for number in coordinator.data or {}:
            if number in known_numbers:
                continue
            known_numbers.add(number)
            for description in SENSOR_DESCRIPTIONS:
                new_entities.append(PodaSensor(coordinator, entry, number, description))
        if new_entities:
            async_add_entities(new_entities)

    _add_new_numbers()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_numbers))


class PodaSensor(CoordinatorEntity[PodaDataCoordinator], SensorEntity):
    """Representation of a single PODA statistic for a single number."""

    entity_description: PodaSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PodaDataCoordinator,
        entry: ConfigEntry,
        number: str,
        description: PodaSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._number = number
        self._attr_unique_id = f"{entry.entry_id}_{number}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, number)},
            name=self._device_name,
            manufacturer="PODA a.s.",
            model="Mobilní tarif",
        )

    @property
    def _device_name(self) -> str:
        stats = (self.coordinator.data or {}).get(self._number)
        if stats and stats.name:
            return f"PODA {self._number} ({stats.name})"
        return f"PODA {self._number}"

    @property
    def native_value(self) -> float | int | None:
        stats = (self.coordinator.data or {}).get(self._number)
        if stats is None:
            return None
        return self.entity_description.value_fn(stats)

    @property
    def extra_state_attributes(self) -> dict[str, str | int]:
        stats = (self.coordinator.data or {}).get(self._number)
        if stats is None:
            return {}
        return {
            "cislo": self._number,
            "nazev": stats.name or "",
            "pocet_hovoru": stats.calls_count,
        }
