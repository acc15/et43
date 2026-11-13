"""Minimal SCPI client for EastTester ET43x LCR meters (incl. ET433).

The ET43x series exposes a virtual serial port (USB CDC) and speaks a small
SCPI dialect.  This module exposes the device commands as named properties and
returns raw measurement values; higher-level helpers live in ``scripts/``.
"""

import contextlib
import os
import time
from collections.abc import Generator
from dataclasses import dataclass
from typing import NamedTuple, Self, override

import serial
from serial.serialutil import Timeout
from serial.tools import list_ports

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

    A tuple, so ``primary, secondary = lcr.fetch()`` works.  ``secondary`` is
    ``None`` when the instrument returns a single value (e.g. in DCR mode).
    """

    primary: float
    secondary: float | None


class ET43:
    """Client for an EastTester ET43x LCR meter.

    Parameters
    ----------
    port:
        A ready-to-use, already open ``serial.Serial`` instance.  The client
        takes ownership: ``close()`` (also called by the context manager)
        always closes it.

    The constructor performs no I/O: it only takes ownership of the port.
    Use :meth:`open` for discovery and port creation, then
    :meth:`auto_remote` for a block that enters remote mode and always returns
    the instrument to local control.

    Notes
    -----
    The client is not thread-safe.  One instance is meant to be used by one
    thread at a time; serialize access externally if needed.
    """

    def __init__(self, port: serial.Serial) -> None:
        self.port = port
        # A single byte read after a CR when it was not followed by LF.
        # It belongs to the next response and must be consumed by the next
        # ``_read()`` call.  One byte is enough because ``_read()`` consumes
        # the stream one character at a time.
        self._pending_byte: bytes | None = None

    # ------------------------------------------------------------------
    # Connection handling

    @classmethod
    def open(
        cls,
        port: str | None = None,
        baudrate: int = 115_200,
        *,
        timeout: float = 2.0,
        exclusive: bool = True,
    ) -> ET43:
        """Open a serial port and return a connected client.

        When ``port`` is omitted, :meth:`discover` is used.  The returned client
        owns the port and closes it in ``close()``.
        """
        if port is None:
            port, baudrate = cls.discover(baudrates=(baudrate, 9_600))

        link = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=timeout,
            write_timeout=timeout,
            exclusive=exclusive and os.name == "posix",
        )

        return cls(link)

    @classmethod
    def discover(
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
                for item in list_ports.comports()
                if item.vid == 0x1A86
                or "CH340" in (item.description or "").upper()
                or "USB SERIAL" in (item.description or "").upper()
            ]
            if not ports:
                raise ConnectionError("ET43x not found: no USB serial adapter detected")

        if not ports:
            raise ConnectionError("ET43x not found: no ports to probe")

        for device in ports:
            for baudrate in baudrates:
                try:
                    options: dict[str, object] = {
                        "port": device,
                        "baudrate": baudrate,
                        "timeout": timeout,
                    }
                    if os.name == "posix":
                        options["exclusive"] = True
                    with serial.Serial(**options) as link:  # type: ignore[arg-type]
                        time.sleep(0.1)
                        link.reset_input_buffer()
                        link.write(b"*IDN?\n")
                        link.flush()
                        time.sleep(0.15)
                        answer = link.read(512)
                except serial.SerialException, OSError:
                    continue
                if b"ET43" in answer.upper():
                    return device, baudrate

        raise ConnectionError(f"ET43x not found: no answer on {', '.join(ports)}")

    def close(self) -> None:
        """Close the serial port."""
        self.port.close()

    @property
    def is_open(self) -> bool:
        """Whether the serial port is currently open."""
        return bool(self.port.is_open)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Low-level serial / SCPI plumbing

    def _write(self, command: str) -> None:
        """Write a command without reading a response."""
        self._pending_byte = None
        self.port.reset_input_buffer()
        self.port.write((command + "\n").encode("ascii"))
        self.port.flush()

    def _read(self, timeout: float | None = None) -> str:
        """Read one response; raise on protocol errors or missing data.

        ``timeout=None`` uses the port's configured timeout.  Only calibration
        passes an explicit value, because ``CORR:EXEC`` takes tens of seconds.
        """
        port_timeout = self.port.timeout
        if timeout is not None:
            self.port.timeout = timeout
        deadline = Timeout(self.port.timeout)
        buffer = bytearray()

        try:
            while True:
                if self._pending_byte is not None:
                    char = self._pending_byte
                    self._pending_byte = None
                else:
                    char = self.port.read(1)

                if not char:
                    break
                if char in (b"\r", b"\n"):
                    if buffer:
                        # A normal device sends CRLF; consume the LF part
                        # so the next read does not see it as an empty line.
                        if char == b"\r" and self.port.in_waiting:
                            extra = self.port.read(1)
                            if extra and extra != b"\n":
                                self._pending_byte = extra
                        break
                    # Ignore leading / duplicated terminators.
                    continue
                buffer.extend(char)
                if deadline.expired():
                    break
        finally:
            if timeout is not None:
                self.port.timeout = port_timeout

        response = buffer.decode("ascii", errors="replace").strip()
        # ET433 answers "cmd err" / "exec err"; ET44.py also expects
        # "execu err" from the ET44/ET45, so match any "... err" reply.
        if response.endswith(" err"):
            raise RuntimeError(response)
        if not response:
            raise TimeoutError("no response (timeout)")
        return response

    def _request(self, command: str, *, timeout: float | None = None) -> str:
        """Write a command and read its response.

        ``timeout`` overrides the port timeout for this response only.
        """
        self._write(command)
        return self._read(timeout)

    def _command(self, command: str, *, timeout: float | None = None) -> None:
        """Write a command and require an ``exec success`` acknowledgement.

        ``timeout`` overrides the port timeout for this response only.
        """
        response = self._request(command, timeout=timeout)
        if response != "exec success":
            raise RuntimeError(f"unexpected response to {command!r}: {response!r}")

    # ------------------------------------------------------------------
    # Identification and display

    @property
    def idn(self) -> Idn:
        """Query ``*IDN?`` and return the instrument identification."""
        raw = self._request("*IDN?")
        fields = [item.strip() for item in raw.split(",")]
        if len(fields) != 5:
            raise ValueError(f"cannot parse *IDN? response: {raw!r}")
        return Idn(*fields, raw=raw)

    @property
    def display_page(self) -> DisplayPage:
        """Current display page: ``MEAS`` or ``SYST``."""
        return DisplayPage.from_string(self._request("DISP:PAGE?"))

    @display_page.setter
    def display_page(self, page: DisplayPage) -> None:
        self._command(f"DISP:PAGE {page.value}")

    # ------------------------------------------------------------------
    # Measurement configuration

    @property
    def mode(self) -> PrimaryMode:
        """Primary parameter: ``AUTO``, ``R``, ``C``, ``L``, ``Z``, ``DCR``, ``ECAP``."""
        return PrimaryMode.from_string(self._request("FUNC:IMP:A?"))

    @mode.setter
    def mode(self, mode: PrimaryMode) -> None:
        self._command(f"FUNC:IMP:A {mode.value}")

    @property
    def secondary(self) -> SecondaryMode:
        """Secondary parameter: ``X``, ``D``, ``Q``, ``THR`` (phase) or ``ESR``."""
        return SecondaryMode.from_string(self._request("FUNC:IMP:B?"))

    @secondary.setter
    def secondary(self, mode: SecondaryMode) -> None:
        self._command(f"FUNC:IMP:B {mode.value}")

    @property
    def equivalent(self) -> Equivalent:
        """Equivalent circuit: ``SER`` (series) or ``PAR`` (parallel)."""
        return Equivalent.from_string(self._request("FUNC:IMP:EQU?"))

    @equivalent.setter
    def equivalent(self, eq: Equivalent) -> None:
        self._command(f"FUNC:IMP:EQU {eq.value}")

    @property
    def frequency(self) -> float:
        """Test frequency in Hz."""
        return float(self._request("FREQ?"))

    @frequency.setter
    def frequency(self, value: float) -> None:
        self._command(f"FREQ {value:g}")

    @property
    def level_mv(self) -> float:
        """Test signal level in mV."""
        return float(self._request("VOLT?"))

    @level_mv.setter
    def level_mv(self, value: float) -> None:
        self._command(f"VOLT {value:g}")

    @property
    def bias_mv(self) -> float:
        """Internal DC bias in mV."""
        return float(self._request("BIAS:VOLT?"))

    @bias_mv.setter
    def bias_mv(self, value: float) -> None:
        self._command(f"BIAS:VOLT {value:g}")

    @property
    def speed(self) -> Speed:
        """Measurement speed: ``FAST``, ``MED`` or ``SLOW``."""
        return Speed.from_string(self._request("APER?"))

    @speed.setter
    def speed(self, speed: Speed) -> None:
        self._command(f"APER {speed.value}")

    @property
    def measurement_range(self) -> int | None:
        """Current impedance range in ohms, or ``None`` for automatic range."""
        answer = self._request("FUNC:IMP:RANGE?")
        return None if answer == "AUTO" else int(answer)

    @measurement_range.setter
    def measurement_range(self, value: int | None) -> None:
        command = "AUTO" if value is None else str(value)
        self._command(f"FUNC:IMP:RANGE {command}")

    @property
    def relative_mode(self) -> bool:
        """Whether relative (deviation) measurement mode is enabled."""
        return self._request("FUNC:DEV:MODE?") == "ON"

    @relative_mode.setter
    def relative_mode(self, value: bool) -> None:
        self._command(f"FUNC:DEV:MODE {'ON' if value else 'OFF'}")

    # ------------------------------------------------------------------
    # Measurements and control

    def fetch(self) -> Reading:
        """Read one ``FETCH?`` response as :class:`Reading`.

        The instrument returns either one value (e.g. in DCR mode) or two.
        Modes and units are not part of the response, so no extra queries are
        made.  ``secondary`` is ``None`` when the device returns one value.
        """
        answer = self._request("FETCH?")
        # Verified on ET433 V2.00.2414.001: values are comma-separated
        # ("2.820165e+04,6.536892e+02").
        match [float(value) for value in answer.split(",")]:
            case [primary]:
                return Reading(primary, None)
            case [primary, secondary]:
                return Reading(primary, secondary)
        raise ValueError(f"cannot parse FETCH? response: {answer!r}")

    def calibrate(self, *, timeout: float = CALIBRATION_TIMEOUT) -> None:
        """Run open- or short-circuit correction (depends on the fixture).

        The instrument decides whether it sees an open or a short, so connect
        the appropriate fixture first.  Repeat once for open and once for short.
        **This overwrites the open/short correction data.**

        The command takes tens of seconds and answers ``exec success`` when
        done.  The method waits up to ``timeout`` seconds (default
        :data:`CALIBRATION_TIMEOUT`, 45 s) for that acknowledgement.
        """
        self._command("CORR:EXEC", timeout=timeout)

    def remote(self) -> None:
        """Enter remote mode (front panel locked)."""
        self._command("SYST:REMOTE")

    def local(self) -> None:
        """Return the instrument to local (front-panel) control."""
        # SYST:LOCAL is not acknowledged by the firmware.
        self._write("SYST:LOCAL")

    @contextlib.contextmanager
    def auto_remote(self) -> Generator[Self]:
        """Use remote mode for the duration of the block and close the port.

        Sends ``SYST:REMOTE`` on entry and ``SYST:LOCAL`` on exit, including
        when the block raises, then closes the serial port.  This keeps the
        front panel unlocked after the session::

            with ET43.open().auto_remote() as lcr:
                ...

        ``SYST:LOCAL`` is also sent when ``SYST:REMOTE`` itself fails: the
        instrument may have applied it and only the acknowledgement was lost.
        When the block (or entering remote) raises, a failure of
        ``SYST:LOCAL`` is suppressed so the original error propagates.

        The port is closed on exit, so call this right on :meth:`open` rather
        than on a client you intend to keep using afterwards.
        """
        with self:
            try:
                self.remote()
                yield self
            except BaseException:
                # Best effort: do not mask the original error.
                with contextlib.suppress(Exception):
                    self.local()
                raise
            else:
                self.local()
