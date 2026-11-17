"""Minimal async SCPI client for EastTester ET43x LCR meters (incl. ET433).

The ET43x series exposes a virtual serial port (USB CDC) and speaks a small
SCPI dialect.  This module exposes the device commands as named coroutines and
returns raw measurement values; higher-level helpers live in ``scripts/``.
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import NamedTuple, Self, override

import serialx

from .enums import (
    DisplayPage,
    Equivalent,
    PrimaryMode,
    SecondaryMode,
    Speed,
)

CALIBRATION_TIMEOUT = 45.0
"""Default maximum wait for the ``CORR:EXEC`` acknowledgement, in seconds."""


@dataclass(frozen=True, slots=True)
class Idn:
    """Contents of ``*IDN?``."""

    manufacturer: str
    model: str
    firmware: str
    hardware: str
    serial: str
    raw: str

    @override
    def __str__(self) -> str:
        return (
            f"{self.manufacturer} {self.model} (firmware {self.firmware}, hardware {self.hardware}, S/N {self.serial})"
        )


class Reading(NamedTuple):
    """One ``FETCH?`` result.

    A tuple, so ``primary, secondary = await lcr.fetch()`` works.
    ``secondary`` is ``None`` when the instrument returns a single value (e.g.
    in DCR mode).
    """

    primary: float
    secondary: float | None


class ET43:
    """Async client for an EastTester ET43x LCR meter.

    Parameters
    ----------
    port:
        A ready-to-use, already open ``serialx.AsyncSerial`` instance.  The
        client takes ownership: ``close()`` (also called by the context
        manager) always closes it.
    timeout:
        Default response timeout in seconds.
    settle:
        Delay after a local-mode setter, in seconds.  In remote mode setters are
        paced by the ``exec success`` acknowledgement and this value is unused.

    The constructor performs no I/O: it only takes ownership of the port.
    Use :meth:`open` for discovery and port creation, then
    :meth:`auto_remote` for a block that enters remote mode and always returns
    the instrument to local control.

    Notes
    -----
    Requests are serialized with an ``asyncio.Lock``, so several tasks may
    share one client.  A sequence of separate commands is not atomic: use your
    own lock around multi-command sequences if they must not interleave.

    The client tracks whether the instrument is in remote mode.  Setters wait
    for an acknowledgement in remote mode and are sent fire-and-forget in
    local mode, where the ET433 applies them without replying.  The client
    assumes local mode after opening the port; use :meth:`remote` or
    :meth:`auto_remote` to enable acknowledgement checks.
    """

    def __init__(
        self,
        port: serialx.AsyncSerial,
        *,
        timeout: float = 2.0,
        settle: float = 0.05,
    ) -> None:
        self.port = port
        self.timeout = timeout
        self.settle = settle
        self._lock = asyncio.Lock()
        self._remote = False

    # ------------------------------------------------------------------
    # Connection handling

    @classmethod
    async def open(
        cls,
        port: str | None = None,
        baudrate: int = 115_200,
        *,
        timeout: float = 2.0,
        settle: float = 0.05,
        exclusive: bool = True,
    ) -> ET43:
        """Open a serial port and return a connected client.

        When ``port`` is omitted, :meth:`discover` is used.  The returned client
        owns the port and closes it in ``close()``.

        ``port`` may also be a serialx URL such as ``socket://host:port`` or
        ``rfc2217://host:port``.

        ``settle`` is the delay after a local-mode setter, in seconds.  The
        instrument does not acknowledge settings in local mode and needs a
        moment before accepting the next command; in remote mode the
        acknowledgement provides that pacing.  Set it to 0 to disable the
        delay.
        """
        if port is None:
            port, baudrate = await cls.discover(baudrates=(baudrate, 9_600))

        link = serialx.async_serial_for_url(
            port,
            baudrate=baudrate,
            exclusive=exclusive and os.name == "posix",
        )
        await link.open()
        return cls(link, timeout=timeout, settle=settle)

    @classmethod
    async def discover(
        cls,
        ports: list[str] | None = None,
        baudrates: tuple[int, ...] = (115_200, 9_600),
        timeout: float = 0.8,
    ) -> tuple[str, int]:
        """Find a connected ET43x and return ``(port, baudrate)``.

        Only USB serial adapters are probed by default (the ET433 uses a
        CH340); other ports such as built-in ``/dev/ttyS*`` are never touched.
        Pass explicit ``ports`` to search something else.
        """
        if ports is None:
            ports = [
                item.device
                for item in await serialx.async_list_serial_ports()
                if cls._is_supported_adapter(item)
            ]
            if not ports:
                raise ConnectionError("ET43x not found: no USB serial adapter detected")

        if not ports:
            raise ConnectionError("ET43x not found: no ports to probe")

        for device in ports:
            for baudrate in baudrates:
                try:
                    async with serialx.async_serial_for_url(
                        device,
                        baudrate=baudrate,
                        exclusive=os.name == "posix",
                    ) as link:
                        await asyncio.sleep(0.1)
                        answer = await cls(link, timeout=timeout)._request("*IDN?", timeout=timeout)
                except (OSError, serialx.SerialException, TimeoutError, RuntimeError):
                    continue
                if "ET43" in answer.upper():
                    return device, baudrate

        raise ConnectionError(f"ET43x not found: no answer on {', '.join(ports)}")

    @staticmethod
    def _is_supported_adapter(info: serialx.SerialPortInfo) -> bool:
        """Whether a serial port looks like a USB serial adapter."""
        description = " ".join(
            value for value in (info.product, info.manufacturer, info.interface_description) if value is not None
        ).upper()
        return info.vid == 0x1A86 or "CH340" in description or "USB SERIAL" in description

    async def close(self) -> None:
        """Close the serial port."""
        await self.port.close()

    @property
    def is_open(self) -> bool:
        """Whether the serial port is currently open."""
        return bool(self.port.is_open)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Low-level serial / SCPI plumbing

    async def _write(self, command: str) -> None:
        """Discard buffered input, then write a command without reading a reply."""
        await self._drain_input()
        await self.port.write((command + "\n").encode("ascii"))
        await self.port.flush()

    async def _drain_input(self) -> None:
        """Discard bytes that are already buffered.

        This is the async equivalent of pyserial's ``reset_input_buffer()``:
        it never waits for new data.  serialx buffers input in a
        ``StreamReader`` and exposes no public reset, so the bytes are consumed
        normally.
        """
        while True:
            try:
                async with asyncio.timeout(0):
                    data = await self.port.read(4096)
            except TimeoutError:
                return
            if not data:
                return

    async def _read(self, timeout: float | None = None) -> str:
        """Read one response; raise on protocol errors or missing data.

        ``timeout=None`` uses :attr:`timeout`.  Calibration and discovery pass
        explicit values; ``CORR:EXEC`` takes tens of seconds.
        """
        limit = self.timeout if timeout is None else timeout
        buffer = bytearray()

        try:
            async with asyncio.timeout(limit):
                while True:
                    char = await self.port.read(1)
                    if not char:
                        break
                    if char in (b"\r", b"\n"):
                        if buffer:
                            break
                        # Ignore leading / duplicated terminators.  This also
                        # consumes the LF that follows a CR in a CRLF reply.
                        continue
                    buffer.extend(char)
        except TimeoutError:
            # Preserve the old pyserial behaviour: a response that arrived
            # without a terminator is still parsed after the timeout.
            if not buffer:
                raise TimeoutError("no response (timeout)") from None

        response = buffer.decode("ascii", errors="replace").strip()
        # ET433 answers "cmd err" / "exec err"; ET44.py also expects
        # "execu err" from the ET44/ET45, so match any "... err" reply.
        if response.endswith(" err"):
            raise RuntimeError(response)
        if not response:
            raise TimeoutError("no response (timeout)")
        return response

    async def _request(self, command: str, *, timeout: float | None = None) -> str:
        """Write a command and read its response.

        ``timeout`` overrides the client timeout for this response only.
        Requests are serialized, so concurrent tasks cannot interleave them.
        """
        async with self._lock:
            await self._write(command)
            return await self._read(timeout)

    async def _command(self, command: str, *, timeout: float | None = None) -> None:
        """Write a command and require an ``exec success`` acknowledgement.

        ``timeout`` overrides the client timeout for this response only.
        """
        response = await self._request(command, timeout=timeout)
        if response != "exec success":
            raise RuntimeError(f"unexpected response to {command!r}: {response!r}")

    async def _set(self, command: str) -> None:
        """Apply a setting.

        In remote mode the instrument acknowledges configuration commands, so
        wait for ``exec success`` and report ``exec err``.  In local mode it
        applies settings without replying, so send them without waiting.
        """
        if self._remote:
            await self._command(command)
            return

        async with self._lock:
            await self._write(command)
            if self.settle > 0:
                await asyncio.sleep(self.settle)

    # ------------------------------------------------------------------
    # Identification and display

    async def idn(self) -> Idn:
        """Query ``*IDN?`` and return the instrument identification."""
        raw = await self._request("*IDN?")
        fields = [item.strip() for item in raw.split(",")]
        if len(fields) != 5:
            raise ValueError(f"cannot parse *IDN? response: {raw!r}")
        return Idn(*fields, raw=raw)

    async def get_display_page(self) -> DisplayPage:
        """Current display page: ``MEAS`` or ``SYST``."""
        return DisplayPage.from_string(await self._request("DISP:PAGE?"))

    async def set_display_page(self, page: DisplayPage) -> None:
        await self._set(f"DISP:PAGE {page.value}")

    # ------------------------------------------------------------------
    # Measurement configuration

    async def get_mode(self) -> PrimaryMode:
        """Primary parameter: ``AUTO``, ``R``, ``C``, ``L``, ``Z``, ``DCR``, ``ECAP``."""
        return PrimaryMode.from_string(await self._request("FUNC:IMP:A?"))

    async def set_mode(self, mode: PrimaryMode) -> None:
        await self._set(f"FUNC:IMP:A {mode.value}")

    async def get_secondary(self) -> SecondaryMode:
        """Secondary parameter: ``X``, ``D``, ``Q``, ``THR`` (phase) or ``ESR``."""
        return SecondaryMode.from_string(await self._request("FUNC:IMP:B?"))

    async def set_secondary(self, mode: SecondaryMode) -> None:
        await self._set(f"FUNC:IMP:B {mode.value}")

    async def get_equivalent(self) -> Equivalent:
        """Equivalent circuit: ``SER`` (series) or ``PAR`` (parallel)."""
        return Equivalent.from_string(await self._request("FUNC:IMP:EQU?"))

    async def set_equivalent(self, eq: Equivalent) -> None:
        await self._set(f"FUNC:IMP:EQU {eq.value}")

    async def get_frequency(self) -> float:
        """Test frequency in Hz."""
        return float(await self._request("FREQ?"))

    async def set_frequency(self, value: float) -> None:
        await self._set(f"FREQ {value:g}")

    async def get_level_mv(self) -> float:
        """Test signal level in mV."""
        return float(await self._request("VOLT?"))

    async def set_level_mv(self, value: float) -> None:
        await self._set(f"VOLT {value:g}")

    async def get_bias_mv(self) -> float:
        """Internal DC bias in mV."""
        return float(await self._request("BIAS:VOLT?"))

    async def set_bias_mv(self, value: float) -> None:
        await self._set(f"BIAS:VOLT {value:g}")

    async def get_speed(self) -> Speed:
        """Measurement speed: ``FAST``, ``MED`` or ``SLOW``."""
        return Speed.from_string(await self._request("APER?"))

    async def set_speed(self, speed: Speed) -> None:
        await self._set(f"APER {speed.value}")

    async def get_measurement_range(self) -> int:
        """Current impedance range in ohms, or ``0`` for automatic range."""
        answer = await self._request("FUNC:IMP:RANGE?")
        return 0 if answer == "AUTO" else int(answer)

    async def set_measurement_range(self, value: int) -> None:
        command = "AUTO" if value == 0 else str(value)
        await self._set(f"FUNC:IMP:RANGE {command}")

    async def get_relative_mode(self) -> bool:
        """Whether relative (deviation) measurement mode is enabled."""
        return await self._request("FUNC:DEV:MODE?") == "ON"

    async def set_relative_mode(self, value: bool) -> None:
        await self._set(f"FUNC:DEV:MODE {'ON' if value else 'OFF'}")

    # ------------------------------------------------------------------
    # Measurements and control

    async def fetch(self) -> Reading:
        """Read one ``FETCH?`` response as :class:`Reading`.

        The instrument returns either one value (e.g. in DCR mode) or two.
        Modes and units are not part of the response, so no extra queries are
        made.  ``secondary`` is ``None`` when the device returns one value.
        """
        answer = await self._request("FETCH?")
        # Verified on ET433 V2.00.2414.001: values are comma-separated
        # ("2.820165e+04,6.536892e+02").
        match [float(value) for value in answer.split(",")]:
            case [primary]:
                return Reading(primary, None)
            case [primary, secondary]:
                return Reading(primary, secondary)
        raise ValueError(f"cannot parse FETCH? response: {answer!r}")

    async def configure(
        self,
        mode: PrimaryMode | None = None,
        secondary: SecondaryMode | None = None,
        equivalent: Equivalent | None = None,
        frequency: float | None = None,
        level_mv: float | None = None,
        bias_mv: float | None = None,
        speed: Speed | None = None,
        measurement_range: int | None = None,
        relative_mode: bool | None = None,
        delay: float = 1.0,
    ) -> None:
        """Apply several settings in one call.

        Every argument is optional: only the ones given are changed, in a
        sensible order.  ``measurement_range=0`` means ``AUTO``; omit it to
        leave it untouched.
        ``delay`` is the stabilisation time after applying everything (1 s by
        default); set it to 0 to skip.  Setters are confirmed in remote mode
        and fire-and-forget in local mode.
        """
        if mode is not None:
            await self.set_mode(mode)
        if secondary is not None:
            await self.set_secondary(secondary)
        if equivalent is not None:
            await self.set_equivalent(equivalent)
        if frequency is not None:
            await self.set_frequency(frequency)
        if level_mv is not None:
            await self.set_level_mv(level_mv)
        if bias_mv is not None:
            await self.set_bias_mv(bias_mv)
        if speed is not None:
            await self.set_speed(speed)
        if measurement_range is not None:
            await self.set_measurement_range(measurement_range)
        if relative_mode is not None:
            await self.set_relative_mode(relative_mode)
        if delay > 0:
            await asyncio.sleep(delay)

    async def calibrate(self, *, timeout: float = CALIBRATION_TIMEOUT) -> None:
        """Run open- or short-circuit correction (depends on the fixture).

        The instrument decides whether it sees an open or a short, so connect
        the appropriate fixture first.  Repeat once for open and once for short.
        **This overwrites the open/short correction data.**

        The command takes tens of seconds and answers ``exec success`` when
        done.  The method waits up to ``timeout`` seconds (default
        :data:`CALIBRATION_TIMEOUT`, 45 s) for that acknowledgement.
        """
        await self._command("CORR:EXEC", timeout=timeout)

    async def remote(self) -> None:
        """Enter remote mode (front panel locked).

        Subsequent setters wait for ``exec success`` and report ``exec err``.
        """
        await self._command("SYST:REMOTE")
        self._remote = True

    async def local(self) -> None:
        """Return the instrument to local (front-panel) control.

        ``SYST:LOCAL`` is not acknowledged, so this waits :attr:`settle`
        seconds before returning.  Subsequent setters are sent fire-and-forget.
        """
        # SYST:LOCAL is not acknowledged and takes a moment to apply.
        async with self._lock:
            await self._write("SYST:LOCAL")
            if self.settle > 0:
                await asyncio.sleep(self.settle)
        self._remote = False

    @contextlib.asynccontextmanager
    async def auto_remote(self) -> AsyncGenerator[Self]:
        """Use remote mode for the duration of the block and close the port.

        Sends ``SYST:REMOTE`` on entry and ``SYST:LOCAL`` on exit, including
        when the block raises, then closes the serial port.  This keeps the
        front panel unlocked after the session::

            async with (await ET43.open()).auto_remote() as lcr:
                ...

        ``SYST:LOCAL`` is also sent when ``SYST:REMOTE`` itself fails: the
        instrument may have applied it and only the acknowledgement was lost.
        When the block (or entering remote) raises, a failure of
        ``SYST:LOCAL`` is suppressed so the original error propagates.

        The port is closed on exit, so call this right on ``await open()``
        rather than on a client you intend to keep using afterwards.
        """
        async with self:
            try:
                await self.remote()
                yield self
            except BaseException:
                # Best effort: do not mask the original error.
                with contextlib.suppress(Exception):
                    await self.local()
                raise
            else:
                await self.local()
