"""Provides the TTLock LockUpdateCoordinator."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import TypeGuard

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt

from .api import TTLockApi
from .const import DOMAIN, SIGNAL_NEW_DATA, TT_LOCKS
from .models import Features, PassageModeConfig, SensorState, State, WebhookEvent

_LOGGER = logging.getLogger(__name__)


@dataclass
class SensorData:
    """Internal state of the optional door sensor."""

    opened: bool | None = None
    battery: int | None = None
    last_fetched: datetime | None = None

    @property
    def present(self) -> bool:
        """To indicate if a sensor is installed."""
        return self.battery is not None


@dataclass
class LockState:
    """Internal state of the lock as managed by the co-oridinator."""

    name: str
    mac: str
    model: str | None = None
    battery_level: int | None = None
    hardware_version: str | None = None
    firmware_version: str | None = None
    features: Features = Features.from_feature_value(None)
    locked: bool | None = None
    action_pending: bool = False
    last_user: str | None = None
    last_reason: str | None = None
    lock_sound: bool | None = None
    sensor: SensorData | None = None
    auto_lock_seconds: int | None = None
    passage_mode_config: PassageModeConfig | None = None

    def passage_mode_active(self, current_date: datetime = dt.now()) -> bool:
        """Check if passage mode is currently active."""
        if self.passage_mode_config and self.passage_mode_config.enabled:
            current_day = current_date.isoweekday()

            if current_day in self.passage_mode_config.week_days:
                if self.passage_mode_config.all_day:
                    return True

                current_minute = current_date.hour * 60 + current_date.minute
                if (
                    self.passage_mode_config.start_minute
                    <= current_minute
                    < self.passage_mode_config.end_minute
                ):
                    # Active by schedule
                    return True
        return False

    def auto_lock_delay(self, current_date: datetime) -> int | None:
        """Return the auto-lock delay in seconds, or None if auto-lock is currently disabled."""
        if self.auto_lock_seconds is None or self.auto_lock_seconds <= 0:
            return None

        if self.passage_mode_active(current_date):
            return None

        return self.auto_lock_seconds


def sensor_present(instance: SensorData | None) -> TypeGuard[SensorData]:
    """Check if a sensor is present."""
    return instance is not None and instance.present


@contextmanager
def lock_action(controller: LockUpdateCoordinator):
    """Wrap a lock action so that in-progress state is managed correctly."""

    controller.data.action_pending = True
    controller.async_update_listeners()
    try:
        yield
    finally:
        controller.data.action_pending = False
        controller.async_update_listeners()


def lock_coordinators(hass: HomeAssistant, entry: ConfigEntry):
    """Help with entity setup."""
    coordinators: list[LockUpdateCoordinator] = hass.data[DOMAIN][entry.entry_id][
        TT_LOCKS
    ]
    yield from coordinators


def coordinator_for(
    hass: HomeAssistant, entity_id: str
) -> LockUpdateCoordinator | None:
    """Given an entity_id, return the coordinator for that entity."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        for coordinator in lock_coordinators(hass, entry):
            for entity in coordinator.entities:
                if entity.entity_id == entity_id:
                    return coordinator
    return None


class LockUpdateCoordinator(DataUpdateCoordinator[LockState]):
    """Class to manage fetching Toon data from single endpoint."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api: TTLockApi,
        lock_id: int,
    ) -> None:
        """Initialize the update co-ordinator for a single lock."""
        self.api = api
        self.lock_id = lock_id
        self._auto_lock_task: asyncio.Task | None = None
        self._state_lock = asyncio.Lock()

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=timedelta(minutes=15),
        )

        async_dispatcher_connect(self.hass, SIGNAL_NEW_DATA, self._process_webhook_data)

    async def _async_update_data(self) -> LockState:
        """Update data with graceful degradation."""
        if self.data is None:
            # First run, we need to fetch all data
            return await self._full_refresh()
        else:
            # Subsequent runs, handle partial updates gracefully
            return await self._partial_refresh()

    async def _full_refresh(self) -> LockState:
        """Full refresh on first run or after failure."""
        try:
            details = await self.api.get_lock(self.lock_id)

            new_data = LockState(
                name=details.name,
                mac=details.mac,
                model=details.model,
                features=Features.from_feature_value(details.featureValue),
            )

            await self._update_lock_details(new_data, details)
            await self._update_sensor_data(new_data)
            await self._update_lock_state(new_data)
            await self._update_passage_mode(new_data)

            return new_data
        except Exception as err:
            raise UpdateFailed(err) from err

    async def _partial_refresh(self) -> LockState:
        """Partial update with graceful degradation."""
        new_data = deepcopy(self.data)

        try:
            details = await self.api.get_lock(self.lock_id)
            new_data.name = details.name
            new_data.battery_level = details.battery_level
            new_data.hardware_version = details.hardwareRevision
            new_data.firmware_version = details.firmwareRevision
            new_data.auto_lock_seconds = details.autoLockTime
            new_data.lock_sound = bool(details.lockSound)

            # Update features if changed
            if details.featureValue:
                new_data.features = Features.from_feature_value(details.featureValue)
        except Exception as err:
            _LOGGER.warning("Failed to fetch lock details: %s", err)
            # Continue with partial update using cached data

        try:
            await self._update_sensor_data(new_data)
        except Exception as err:
            _LOGGER.debug("Failed to update sensor data: %s", err)

        try:
            await self._update_lock_state(new_data)
        except Exception as err:
            _LOGGER.debug("Failed to update lock state: %s", err)

        try:
            await self._update_passage_mode(new_data)
        except Exception as err:
            _LOGGER.debug("Failed to update passage mode: %s", err)

        return new_data

    async def _update_lock_details(self, new_data: LockState, details) -> None:
        """Update basic lock details."""
        new_data.name = details.name
        new_data.battery_level = details.battery_level
        new_data.hardware_version = details.hardwareRevision
        new_data.firmware_version = details.firmwareRevision

    async def _update_sensor_data(self, new_data: LockState) -> None:
        """Update sensor data if supported."""
        if Features.door_sensor in new_data.features:
            # Make sure we have a placeholder for sensor state
            if new_data.sensor is None:
                new_data.sensor = SensorData()

            # Only fetch sensor metadata once a day
            if (
                new_data.sensor.last_fetched is None
                or new_data.sensor.last_fetched < dt.now() - timedelta(days=1)
            ):
                sensor = await self.api.get_sensor(self.lock_id)
                new_data.sensor.last_fetched = dt.now()
                if sensor:
                    new_data.sensor.battery = sensor.battery_level
        else:
            new_data.sensor = None

    async def _update_lock_state(self, new_data: LockState) -> None:
        """Update lock state if unknown."""
        if new_data.locked is None:
            try:
                state = await self.api.get_lock_state(self.lock_id)
                new_data.locked = state.locked == State.locked
                if sensor_present(new_data.sensor):
                    new_data.sensor.opened = state.opened == SensorState.opened
            except Exception:
                pass

    async def _update_passage_mode(self, new_data: LockState) -> None:
        """Update passage mode configuration."""
        new_data.passage_mode_config = await self.api.get_lock_passage_mode_config(
            self.lock_id
        )

    @callback
    def _process_webhook_data(self, event: WebhookEvent):
        """Update data from webhook event."""
        if event.id != self.lock_id:
            return

        _LOGGER.debug("Lock %s received %s", self.unique_id, event)

        if not event.success:
            return

        if not self.data:
            return

        new_data = deepcopy(self.data)
        new_data.battery_level = event.battery_level

        if state := event.state:
            if state.locked == State.locked:
                new_data.locked = True
                # Cancel auto-lock when manually locked
                if self._auto_lock_task and not self._auto_lock_task.done():
                    _LOGGER.debug("Cancelling auto-lock on manual lock")
                    self._auto_lock_task.cancel()
            elif state.locked == State.unlocked:
                new_data.locked = False
                self._handle_auto_lock(event.lock_ts, event.server_ts)

            if state.locked is not None:
                new_data.last_user = event.user
                new_data.last_reason = event.event.description

        if new_data.sensor and new_data.sensor.present and event.sensorState:
            if event.sensorState.opened == SensorState.opened:
                new_data.sensor.opened = True
            if event.sensorState.opened == SensorState.closed:
                new_data.sensor.opened = False
                new_data.locked = True
                new_data.last_reason = "Door Closed"
                # Cancel auto-lock when door sensor closes
                if self._auto_lock_task and not self._auto_lock_task.done():
                    _LOGGER.debug("Cancelling auto-lock on sensor close")
                    self._auto_lock_task.cancel()

                _LOGGER.debug("Assuming auto-locked via sensor")
        self.async_set_updated_data(new_data)

    def _handle_auto_lock(self, lock_ts: datetime, server_ts: datetime):
        """Handle auto-locking the lock."""

        auto_lock_delay = self.data.auto_lock_delay(lock_ts)
        computed_msg_delay = max(0, (server_ts - lock_ts).total_seconds())

        if auto_lock_delay is None:
            _LOGGER.debug("Auto-lock is disabled")
            return

        # Cancel any existing auto-lock task
        if self._auto_lock_task and not self._auto_lock_task.done():
            _LOGGER.debug("Cancelling previous auto-lock task")
            self._auto_lock_task.cancel()

        async def _auto_locked(seconds: int, offset: float = 0):
            try:
                if seconds > 0 and (seconds - offset) > 0:
                    await asyncio.sleep(seconds - offset)

                # Only apply if still unlocked
                if self.data and not self.data.locked:
                    new_data = deepcopy(self.data)
                    new_data.locked = True
                    new_data.last_reason = "Auto Lock"

                    _LOGGER.debug(
                        "Assuming lock auto locked after %s seconds", auto_lock_delay
                    )
                    self.async_set_updated_data(new_data)
                else:
                    _LOGGER.debug("Auto-lock skipped, lock state changed")
            except asyncio.CancelledError:
                _LOGGER.debug("Auto-lock task cancelled")
                raise

        self._auto_lock_task = self.hass.create_task(
            _auto_locked(auto_lock_delay, computed_msg_delay)
        )

    @property
    def unique_id(self) -> str:
        """Unique ID prefix for all entities for the lock."""
        return f"{DOMAIN}-{self.lock_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Device info for the lock."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.data.mac)},
            manufacturer="TT Lock",
            model=self.data.model,
            name=self.data.name,
            sw_version=self.data.firmware_version,
            hw_version=self.data.hardware_version,
        )

    @property
    def entities(self) -> list[Entity]:
        """Entities belonging to this co-ordinator."""
        return [
            callback.__self__
            for callback, _ in list(self._listeners.values())
            if isinstance(callback.__self__, Entity)
        ]

    def as_dict(self) -> dict:
        """Serialize for diagnostics."""
        return {
            "unique_id": self.unique_id,
            "device": self.data,
            "entities": [
                self.hass.states.get(entity.entity_id).as_dict()
                for entity in self.entities
            ],
        }

    async def lock(self) -> None:
        """Try to lock the lock."""
        async with self._state_lock:
            with lock_action(self):
                res = await self.api.lock(self.lock_id)
                if res:
                    self.data.locked = True
                    # Cancel any pending auto-lock
                    if self._auto_lock_task and not self._auto_lock_task.done():
                        self._auto_lock_task.cancel()

    async def unlock(self) -> None:
        """Try to unlock the lock."""
        async with self._state_lock:
            with lock_action(self):
                res = await self.api.unlock(self.lock_id)
                if res:
                    self.data.locked = False

    async def set_auto_lock(self, on: bool) -> None:
        """Turn on/off Autolock."""
        seconds = 10 if on else 0
        res = await self.api.set_auto_lock(self.lock_id, seconds)
        if res:
            self.data.auto_lock_seconds = seconds
            self.async_update_listeners()

    async def set_lock_sound(self, on: bool) -> None:
        """Turn on/off lock sound."""
        value = 1 if on else 2
        res = await self.api.set_lock_sound(self.lock_id, value)
        if res:
            self.data.lock_sound = on
            self.async_update_listeners()
