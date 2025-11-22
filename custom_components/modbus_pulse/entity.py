"""Base implementation for all modbus platforms."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
import copy
from datetime import datetime, timedelta
import logging
import struct
from typing import Any, cast

from homeassistant.const import (
    CONF_ADDRESS,
    CONF_COMMAND_OFF,
    CONF_COMMAND_ON,
    CONF_COUNT,
    CONF_DELAY,
    CONF_DEVICE_CLASS,
    CONF_NAME,
    CONF_OFFSET,
    CONF_SCAN_INTERVAL,
    CONF_SLAVE,
    CONF_STRUCTURE,
    CONF_UNIQUE_ID,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, ToggleEntity
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CALL_TYPE_COIL,
    CALL_TYPE_DISCRETE,
    CALL_TYPE_REGISTER_HOLDING,
    CALL_TYPE_REGISTER_INPUT,
    CALL_TYPE_WRITE_COIL,
    CALL_TYPE_WRITE_COILS,
    CALL_TYPE_WRITE_REGISTER,
    CALL_TYPE_WRITE_REGISTERS,
    CALL_TYPE_X_COILS,
    CALL_TYPE_X_REGISTER_HOLDINGS,
    CONF_DATA_TYPE,
    CONF_DEVICE_ADDRESS,
    CONF_INPUT_TYPE,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_NAN_VALUE,
    CONF_PRECISION,
    CONF_PULSE,
    CONF_PULSE_DELAY,
    CONF_SCALE,
    CONF_SLAVE_COUNT,
    CONF_STATE_OFF,
    CONF_STATE_ON,
    CONF_SWAP,
    CONF_SWAP_BYTE,
    CONF_SWAP_WORD,
    CONF_SWAP_WORD_BYTE,
    CONF_VERIFY,
    CONF_VIRTUAL_COUNT,
    CONF_WRITE_TYPE,
    CONF_ZERO_SUPPRESS,
    SIGNAL_STOP_ENTITY,
    DataType,
)
from .modbus import ModbusHub

_LOGGER = logging.getLogger(__name__)


class ModbusBaseEntity(Entity):
    """Base for readonly platforms."""

    _value: str | None = None
    _attr_should_poll = False
    _attr_available = True
    _attr_unit_of_measurement = None

    def __init__(
        self, hass: HomeAssistant, hub: ModbusHub, entry: dict[str, Any]
    ) -> None:
        """Initialize the Modbus binary sensor."""

        self._hub = hub
        if (conf_slave := entry.get(CONF_SLAVE)) is not None:
            self._device_address = conf_slave
        else:
            self._device_address = entry.get(CONF_DEVICE_ADDRESS, 1)
        self._address = int(entry[CONF_ADDRESS])
        self._input_type = entry[CONF_INPUT_TYPE]
        self._scan_interval = int(entry[CONF_SCAN_INTERVAL])
        self._cancel_call: Callable[[], None] | None = None
        self._attr_unique_id = entry.get(CONF_UNIQUE_ID)
        self._attr_name = entry[CONF_NAME]
        self._attr_device_class = entry.get(CONF_DEVICE_CLASS)

        self._min_value = entry.get(CONF_MIN_VALUE)
        self._max_value = entry.get(CONF_MAX_VALUE)
        self._nan_value = entry.get(CONF_NAN_VALUE)
        self._zero_suppress = entry.get(CONF_ZERO_SUPPRESS)

    @abstractmethod
    async def _async_update(self) -> None:
        """Virtual function to be overwritten."""

    async def async_update(self, now: datetime | None = None) -> None:
        """Update the entity state."""
        await self.async_local_update(cancel_pending_update=True)

    async def async_local_update(
        self, now: datetime | None = None, cancel_pending_update: bool = False
    ) -> None:
        """Update the entity state."""
        if cancel_pending_update and self._cancel_call:
            self._cancel_call()
        await self._async_update()
        self.async_write_ha_state()
        if self._scan_interval > 0:
            self._cancel_call = async_call_later(
                self.hass,
                timedelta(seconds=self._scan_interval),
                self.async_local_update,
            )

    async def async_will_remove_from_hass(self) -> None:
        """Remove entity from hass."""
        self.async_disable()

    @callback
    def async_disable(self) -> None:
        """Remote stop entity."""
        _LOGGER.info(f"hold entity {self._attr_name}")
        if self._cancel_call:
            self._cancel_call()
            self._cancel_call = None
        self._attr_available = False

    async def async_await_connection(self, _now: Any) -> None:
        """Wait for first connect."""
        await self._hub.event_connected.wait()
        await self.async_local_update(cancel_pending_update=True)

    async def async_base_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        self.async_on_remove(
            async_call_later(
                self.hass,
                self._hub.config_delay + 0.1,
                self.async_await_connection,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_STOP_ENTITY, self.async_disable)
        )


class ModbusStructEntity(ModbusBaseEntity, RestoreEntity):
    """Base class representing a sensor/climate."""

    def __init__(self, hass: HomeAssistant, hub: ModbusHub, config: dict) -> None:
        """Initialize the switch."""
        super().__init__(hass, hub, config)
        self._swap = config[CONF_SWAP]
        self._data_type = config[CONF_DATA_TYPE]
        self._structure: str = config[CONF_STRUCTURE]
        self._scale = config[CONF_SCALE]
        self._offset = config[CONF_OFFSET]
        self._slave_count = config.get(CONF_SLAVE_COUNT) or config.get(
            CONF_VIRTUAL_COUNT, 0
        )
        self._slave_size = self._count = config[CONF_COUNT]
        self._value_is_int: bool = self._data_type in (
            DataType.INT16,
            DataType.INT32,
            DataType.INT64,
            DataType.UINT16,
            DataType.UINT32,
            DataType.UINT64,
        )
        if not self._value_is_int:
            self._precision = config.get(CONF_PRECISION, 2)
        else:
            self._precision = config.get(CONF_PRECISION, 0)
            if self._precision > 0 or self._scale != int(self._scale):
                self._value_is_int = False

    def _swap_registers(self, registers: list[int], slave_count: int) -> list[int]:
        """Do swap as needed."""
        if slave_count:
            swapped = []
            for i in range(self._slave_count + 1):
                inx = i * self._slave_size
                inx2 = inx + self._slave_size
                swapped.extend(self._swap_registers(registers[inx:inx2], 0))
            return swapped
        if self._swap in (CONF_SWAP_BYTE, CONF_SWAP_WORD_BYTE):
            # convert [12][34] --> [21][43]
            for i, register in enumerate(registers):
                registers[i] = int.from_bytes(
                    register.to_bytes(2, byteorder="little"),
                    byteorder="big",
                    signed=False,
                )
        if self._swap in (CONF_SWAP_WORD, CONF_SWAP_WORD_BYTE):
            # convert [12][34] ==> [34][12]
            registers.reverse()
        return registers

    def __process_raw_value(self, entry: float | str | bytes) -> str | None:
        """Process value from sensor with NaN handling, scaling, offset, min/max etc."""
        if self._nan_value is not None and entry in (self._nan_value, -self._nan_value):
            return None
        if isinstance(entry, bytes):
            return entry.decode()
        if entry != entry:  # noqa: PLR0124
            # NaN float detection replace with None
            return None
        val: float | int = self._scale * entry + self._offset
        if self._min_value is not None and val < self._min_value:
            val = self._min_value
        if self._max_value is not None and val > self._max_value:
            val = self._max_value
        if self._zero_suppress is not None and abs(val) <= self._zero_suppress:
            return "0"
        if self._precision == 0:
            return str(round(val))
        return f"{float(val):.{self._precision}f}"

    def unpack_structure_result(self, registers: list[int]) -> str | None:
        """Convert registers to proper result."""

        if self._swap:
            registers = self._swap_registers(
                copy.deepcopy(registers), self._slave_count
            )
        byte_string = b"".join([x.to_bytes(2, byteorder="big") for x in registers])
        if self._data_type == DataType.STRING:
            return byte_string.decode()
        if byte_string == b"nan\x00":
            return None

        try:
            val = struct.unpack(self._structure, byte_string)
        except struct.error as err:
            recv_size = len(registers) * 2
            msg = f"Received {recv_size} bytes, unpack error {err}"
            _LOGGER.error(msg)
            return None
        if len(val) > 1:
            # Apply scale, precision, limits to floats and ints
            v_result = []
            for entry in val:
                v_temp = self.__process_raw_value(entry)
                if self._data_type != DataType.CUSTOM:
                    v_result.append(str(v_temp))
                else:
                    v_result.append(str(v_temp) if v_temp is not None else "0")
            return ",".join(map(str, v_result))

        # Apply scale, precision, limits to floats and ints
        return self.__process_raw_value(val[0])

class ModbusToggleEntity(ModbusBaseEntity, ToggleEntity, RestoreEntity):
    """
    Pulse-safe Modbus toggle entity (switch/light unified).
    - ON sends clean 1→0 pulse
    - OFF also sends 1→0 pulse (for LOGO NI toggles)
    - Lights and switches both inherit safely
    - _command_off is optional (lights may not have it)
    """

    def __init__(self, hass: HomeAssistant, hub: ModbusHub, config: dict) -> None:
        config[CONF_INPUT_TYPE] = ""
        super().__init__(hass, hub, config)

        self._attr_is_on = False

        # write type conversion
        convert = {
            CALL_TYPE_REGISTER_HOLDING: (
                CALL_TYPE_REGISTER_HOLDING,
                CALL_TYPE_WRITE_REGISTER,
            ),
            CALL_TYPE_DISCRETE: (
                CALL_TYPE_DISCRETE,
                None,
            ),
            CALL_TYPE_REGISTER_INPUT: (
                CALL_TYPE_REGISTER_INPUT,
                None,
            ),
            CALL_TYPE_COIL: (CALL_TYPE_COIL, CALL_TYPE_WRITE_COIL),
            CALL_TYPE_X_COILS: (CALL_TYPE_COIL, CALL_TYPE_WRITE_COILS),
            CALL_TYPE_X_REGISTER_HOLDINGS: (
                CALL_TYPE_REGISTER_HOLDING,
                CALL_TYPE_WRITE_REGISTERS,
            ),
        }
        self._write_type = convert[config[CONF_WRITE_TYPE]][1]

        # Commands
        self.command_on = config.get(CONF_COMMAND_ON, 1)
        self._command_off = config.get(CONF_COMMAND_OFF, 0)

        # Pulse settings
        self._pulse = config.get(CONF_PULSE, False)
        self._pulse_delay = config.get(CONF_PULSE_DELAY, 0) / 1000

        # Verification settings
        if CONF_VERIFY in config:
            verify = config[CONF_VERIFY] or {}
            self._verify_active = True
            self._verify_delay = verify.get(CONF_DELAY, 0)
            self._verify_address = verify.get(CONF_ADDRESS, config[CONF_ADDRESS])
            self._verify_type = convert[
                verify.get(CONF_INPUT_TYPE, config[CONF_WRITE_TYPE])
            ][0]
            self._state_on = verify.get(CONF_STATE_ON, 1)
            self._state_off = verify.get(CONF_STATE_OFF, 0)
        else:
            self._verify_active = False

    #
    # SAFE OFF VALUE FOR LIGHTS (they may not define _command_off)
    #
    def _off_value(self) -> int:
        return getattr(self, "_command_off", 0)

    async def async_added_to_hass(self) -> None:
        await self.async_base_added_to_hass()
        if state := await self.async_get_last_state():
            self._attr_is_on = (state.state == STATE_ON)

    #
    # --------------------- CLEAN PULSE LOGIC ---------------------
    #
    async def _do_pulse(self) -> None:
        """
        Send a 1→0 pulse to the configured Modbus coil.
        Used for both ON and OFF because LOGO toggles on rising edges.
        """
        # Rising edge
        await self._hub.async_pb_call(
            self._device_address,
            self._address,
            self.command_on,
            self._write_type,
        )

        async def _finish(_now):
            # Falling edge
            await self._hub.async_pb_call(
                self._device_address,
                self._address,
                self._off_value(),
                self._write_type,
            )

            if self._verify_active:
                await self.async_update()
            else:
                await self.async_local_update(cancel_pending_update=True)

        async_call_later(self.hass, self._pulse_delay, _finish)

    #
    # ---------------------- PUBLIC ACTIONS -----------------------
    #
    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._pulse:
            await self._do_pulse()
            return

        await self._write_direct(self.command_on)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._pulse:
            # LOGO toggles → pulse even for OFF
            await self._do_pulse()
            return

        await self._write_direct(self._off_value())

    #
    # --------------- COMPATIBILITY WITH ModbusLight ---------------
    #
    async def async_turn(self, command: int) -> None:
        """
        Lights call async_turn(command) instead of async_turn_on/off.
        Ensure pulse handling works the same.
        """
        if self._pulse:
            await self._do_pulse()
            return

        await self._write_direct(command)

    #
    # ---------------- DIRECT WRITE (NON-PULSE) --------------------
    #
    async def _write_direct(self, value: int) -> None:
        result = await self._hub.async_pb_call(
            self._device_address,
            self._address,
            value,
            self._write_type,
        )
        if result is None:
            self._attr_available = False
            self.async_write_ha_state()
            return

        self._attr_available = True

        if not self._verify_active:
            self._attr_is_on = (value == self.command_on)
            self.async_write_ha_state()
            return

        # verify after delay or immediately
        if self._verify_delay:
            async_call_later(self.hass, self._verify_delay, self.async_update)
        else:
            await self.async_update()

    #
    # ------------------------ VERIFICATION -------------------------
    #
    async def async_update(self, _now: datetime | None = None) -> None:
        if not self._verify_active:
            return

        result = await self._hub.async_pb_call(
            self._device_address,
            self._verify_address,
            1,
            self._verify_type,
        )
        if result is None:
            self._attr_available = False
            self.async_write_ha_state()
            return

        self._attr_available = True

        if self._verify_type in (CALL_TYPE_COIL, CALL_TYPE_DISCRETE):
            self._attr_is_on = bool(result.bits[0] & 1)
        else:
            val = int(result.registers[0])
            self._attr_is_on = (val == self._state_on)

        self.async_write_ha_state()
