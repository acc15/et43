import asyncio
import time
from types import SimpleNamespace
from typing import Self, cast

import pytest
import serialx

from et43.client import CALIBRATION_TIMEOUT, ET43, Reading
from et43.enums import (
    DisplayPage,
    Equivalent,
    PrimaryMode,
    SecondaryMode,
    Speed,
)


class _FakePort:
    """Minimal stand-in for ``serialx.AsyncSerial`` in offline tests."""

    def __init__(
        self,
        is_open: bool = True,
        read_data: bytes = b"",
        *,
        response: bytes = b"",
        read_delay: float = 0.0,
        eof: bool = False,
    ) -> None:
        self.is_open = is_open
        self.read_data = bytearray(read_data)
        self.response = bytearray(response)
        self.read_delay = read_delay
        self.eof = eof
        self.writes: list[bytes] = []
        self._closed = asyncio.Event()

    async def read(self, size: int = 1) -> bytes:
        if not self.is_open:
            raise OSError("port is closed")
        if not self.read_data:
            # Simulate an instrument that does not answer: wait until the
            # caller's asyncio.timeout() cancels the read or the port is closed.
            if self.eof:
                return b""
            await self._closed.wait()
        if self.read_delay:
            delay, self.read_delay = self.read_delay, 0.0
            await asyncio.sleep(delay)
        data = bytes(self.read_data[:size])
        del self.read_data[:size]
        return data

    async def write(self, data: bytes) -> None:
        if not self.is_open:
            raise OSError("port is closed")
        self.writes.append(data)
        if self.response:
            self.read_data.extend(self.response)
            self.response.clear()

    async def flush(self) -> None:
        pass

    async def close(self) -> None:
        self.is_open = False
        self._closed.set()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()


def _attach(port: _FakePort, *, timeout: float = 2.0, settle: float = 0.05) -> ET43:
    """Create a client that owns *port*."""
    return ET43(cast(serialx.AsyncSerial, port), timeout=timeout, settle=settle)


def _record_commands(client: ET43, commands: list[str]) -> None:
    async def command(command: str, *, timeout: float | None = None) -> None:
        commands.append(command)

    client._command = command  # type: ignore[method-assign]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("c", PrimaryMode.C),
        ("C", PrimaryMode.C),
        ("auto", PrimaryMode.AUTO),
        ("Cp", PrimaryMode.C),
        ("Rs", PrimaryMode.R),
        ("DCR", PrimaryMode.DCR),
        (PrimaryMode.R, PrimaryMode.R),
    ],
)
def test_primary_mode_from_string(value: str | PrimaryMode, expected: PrimaryMode) -> None:
    assert PrimaryMode.from_string(value) == expected


def test_primary_mode_rejects_secondary() -> None:
    with pytest.raises(ValueError):
        PrimaryMode.from_string("ESR")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("thr", SecondaryMode.THR),
        ("ESR", SecondaryMode.ESR),
        ("D", SecondaryMode.D),
        ("THR", SecondaryMode.THR),
        (SecondaryMode.Q, SecondaryMode.Q),
    ],
)
def test_secondary_mode_from_string(value: str | SecondaryMode, expected: SecondaryMode) -> None:
    assert SecondaryMode.from_string(value) == expected


def test_secondary_mode_rejects_primary() -> None:
    with pytest.raises(ValueError):
        SecondaryMode.from_string("C")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ser", Equivalent.SER),
        ("PAL", Equivalent.PAR),
        ("PALlel", Equivalent.PAR),
        ("par", Equivalent.PAR),
        ("SERial", Equivalent.SER),
        (Equivalent.SER, Equivalent.SER),
    ],
)
def test_equivalent_from_string(value: str | Equivalent, expected: Equivalent) -> None:
    assert Equivalent.from_string(value) == expected


def test_equivalent_par_uses_firmware_token() -> None:
    assert Equivalent.PAR.value == "PAL"


def test_equivalent_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        Equivalent.from_string("PARALLEL")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("fast", Speed.FAST),
        ("MED", Speed.MED),
        ("MEDium", Speed.MED),
        ("MEDIUM", Speed.MED),
        (Speed.SLOW, Speed.SLOW),
    ],
)
def test_speed_from_string(value: str | Speed, expected: Speed) -> None:
    assert Speed.from_string(value) == expected


async def test_discover_without_ports_raises_connection_error() -> None:
    with pytest.raises(ConnectionError):
        await ET43.discover(ports=[])


async def test_discover_skips_non_usb_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    builtin = [
        SimpleNamespace(
            device=f"/dev/ttyS{i}",
            vid=None,
            product=None,
            manufacturer=None,
            interface_description=None,
        )
        for i in range(32)
    ]

    async def fake_ports() -> list[SimpleNamespace]:
        return builtin

    monkeypatch.setattr(serialx, "async_list_serial_ports", fake_ports)

    def fail_open(*_: object, **__: object) -> None:
        pytest.fail("non-USB ports must not be opened")

    monkeypatch.setattr(serialx, "async_serial_for_url", fail_open)

    with pytest.raises(ConnectionError, match="no USB serial adapter") as info:
        await ET43.discover()
    assert "ttyS" not in str(info.value)


async def test_discover_returns_usb_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    link = _FakePort(response=b"ZC,ET433,V1,H1,SN\n")
    ports = [
        SimpleNamespace(
            device="/dev/fake",
            vid=0x1A86,
            product="USB-SERIAL CH340",
            manufacturer=None,
            interface_description=None,
        )
    ]

    async def fake_ports() -> list[SimpleNamespace]:
        return ports

    monkeypatch.setattr(serialx, "async_list_serial_ports", fake_ports)
    monkeypatch.setattr(serialx, "async_serial_for_url", lambda *_, **__: link)

    assert await ET43.discover() == ("/dev/fake", 115_200)
    assert not link.is_open


def _client(answer: str, responses: dict[str, str] | None = None, calls: list[str] | None = None) -> ET43:
    client = _attach(_FakePort())
    mapping: dict[str, str] = {
        "FUNC:IMP:A?": PrimaryMode.C.value,
        "FUNC:IMP:B?": SecondaryMode.ESR.value,
        "FUNC:IMP:RANGE?": "AUTO",
        "APER?": Speed.MED.value,
        "FETCH?": answer,
        "DISP:PAGE?": DisplayPage.MEAS.value,
    }
    if responses is not None:
        mapping.update(responses)

    async def request(command: str, *, timeout: float | None = None) -> str:
        if calls is not None:
            calls.append(command)
        return mapping[command]

    client._request = request  # type: ignore[method-assign]
    client._remote = True
    return client


async def test_closed_port_state() -> None:
    client = _attach(_FakePort(is_open=False))

    assert client.is_open is False
    await client.close()  # must be idempotent when the port is already closed

    with pytest.raises(OSError):
        await client._request("FREQ?")


async def test_constructor_performs_no_io() -> None:
    fake_port = _FakePort()
    client = ET43(cast(serialx.AsyncSerial, fake_port))

    assert client.is_open is True
    await client.close()
    assert fake_port.is_open is False


async def test_idn_is_queried_each_time() -> None:
    calls: list[str] = []
    client = _attach(_FakePort())

    async def request(command: str, *, timeout: float | None = None) -> str:
        calls.append(command)
        return "ZC,ET433,V1,H1,SN"

    client._request = request  # type: ignore[method-assign]

    assert await client.idn() == await client.idn()
    assert calls == ["*IDN?", "*IDN?"]


async def test_close_owns_port() -> None:
    fake_port = _FakePort()
    client = _attach(fake_port)

    await client.close()
    assert fake_port.writes == []
    assert fake_port.is_open is False


async def test_write_does_not_read() -> None:
    fake_port = _FakePort()
    client = _attach(fake_port)

    await client._write("SYST:LOCAL")
    assert fake_port.writes == [b"SYST:LOCAL\n"]


async def test_remote_sends_command_and_waits_for_ack() -> None:
    fake_port = _FakePort(response=b"exec success\n")
    client = _attach(fake_port)

    await client.remote()
    assert fake_port.writes == [b"SYST:REMOTE\n"]
    assert client._remote is True


async def test_equivalent_par_sends_firmware_token() -> None:
    fake_port = _FakePort(response=b"exec success\n")
    client = _attach(fake_port)
    client._remote = True

    await client.set_equivalent(Equivalent.PAR)
    assert fake_port.writes == [b"FUNC:IMP:EQU PAL\n"]


async def test_equivalent_parses_pallel_response() -> None:
    fake_port = _FakePort(response=b"PALlel\n")
    client = _attach(fake_port)

    assert await client.get_equivalent() is Equivalent.PAR


async def test_local_writes_without_reading_ack() -> None:
    fake_port = _FakePort()
    client = _attach(fake_port)
    client._remote = True

    await client.local()
    assert fake_port.writes == [b"SYST:LOCAL\n"]
    assert client._remote is False


async def test_local_sleeps_settle() -> None:
    fake_port = _FakePort()
    client = _attach(fake_port, settle=0.02)
    client._remote = True

    started = time.monotonic()
    await client.local()

    assert time.monotonic() - started >= 0.015
    assert fake_port.writes == [b"SYST:LOCAL\n"]
    assert client._remote is False


async def test_local_without_settle_does_not_wait() -> None:
    fake_port = _FakePort()
    client = _attach(fake_port, settle=0)
    client._remote = True

    async with asyncio.timeout(0.01):
        await client.local()

    assert fake_port.writes == [b"SYST:LOCAL\n"]
    assert client._remote is False


async def test_auto_remote_enters_and_exits() -> None:
    port = _FakePort(response=b"exec success\n")
    client = _attach(port)

    async with client.auto_remote() as current:
        assert current is client
        assert port.writes == [b"SYST:REMOTE\n"]
        assert port.is_open

    assert port.writes == [b"SYST:REMOTE\n", b"SYST:LOCAL\n"]
    assert not port.is_open


async def test_auto_remote_exits_on_exception() -> None:
    port = _FakePort(response=b"exec success\n")
    client = _attach(port)

    with pytest.raises(RuntimeError, match="boom"):
        async with client.auto_remote():
            raise RuntimeError("boom")

    assert port.writes == [b"SYST:REMOTE\n", b"SYST:LOCAL\n"]
    assert not port.is_open


async def test_auto_remote_exits_when_remote_fails() -> None:
    port = _FakePort()  # no acknowledgement -> remote() times out
    client = _attach(port, timeout=0.01)

    with pytest.raises(TimeoutError):
        async with client.auto_remote():
            pytest.fail("block must not run")

    assert port.writes == [b"SYST:REMOTE\n", b"SYST:LOCAL\n"]
    assert not port.is_open


async def test_auto_remote_local_failure_does_not_mask_block_error() -> None:
    port = _FakePort(response=b"exec success\n")
    client = _attach(port)

    async def broken_local() -> None:
        raise OSError("port gone")

    client.local = broken_local  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="boom"):
        async with client.auto_remote():
            raise RuntimeError("boom")

    assert not port.is_open


async def test_auto_remote_local_failure_after_success_propagates() -> None:
    port = _FakePort(response=b"exec success\n")
    client = _attach(port)

    async def broken_local() -> None:
        raise OSError("port gone")

    client.local = broken_local  # type: ignore[method-assign]

    with pytest.raises(OSError, match="port gone"):
        async with client.auto_remote():
            pass

    assert not port.is_open


async def test_calibrate_waits_for_ack() -> None:
    fake_port = _FakePort(response=b"exec success\n")
    client = _attach(fake_port)

    await client.calibrate()
    assert fake_port.writes == [b"CORR:EXEC\n"]


async def test_calibrate_uses_calibration_timeout() -> None:
    client = _attach(_FakePort())
    seen: list[float | None] = []

    async def fake_read(timeout: float | None = None) -> str:
        seen.append(timeout)
        return "exec success"

    client._read = fake_read  # type: ignore[method-assign]
    await client.calibrate()

    assert seen == [CALIBRATION_TIMEOUT]


async def test_calibrate_timeout_override() -> None:
    client = _attach(_FakePort())
    seen: list[float | None] = []

    async def fake_read(timeout: float | None = None) -> str:
        seen.append(timeout)
        return "exec success"

    client._read = fake_read  # type: ignore[method-assign]
    await client.calibrate(timeout=12.5)

    assert seen == [12.5]


async def test_read_accepts_crlf_and_bare_terminators() -> None:
    for reply in (b"1,2\r\n", b"1,2\r", b"1,2\n"):
        assert await _attach(_FakePort(read_data=reply))._read() == "1,2"


async def test_read_returns_next_response_after_crlf() -> None:
    port = _FakePort(read_data=b"1,2\r\n3,4\n")
    client = _attach(port)

    assert await client._read() == "1,2"
    assert await client._read() == "3,4"


async def test_read_preserves_byte_after_bare_cr() -> None:
    port = _FakePort(read_data=b"1,2\r3,4\n")
    client = _attach(port)

    assert await client._read() == "1,2"
    assert await client._read() == "3,4"


async def test_read_uses_explicit_timeout() -> None:
    port = _FakePort(read_data=b"exec success\n", read_delay=0.05)
    client = _attach(port)

    with pytest.raises(TimeoutError):
        await client._read(timeout=0.01)

    assert await client._read() == "exec success"


async def test_read_raises_on_empty_response() -> None:
    client = _attach(_FakePort(), timeout=0.01)

    with pytest.raises(TimeoutError):
        await client._read()


async def test_write_always_drains_stale_input() -> None:
    port = _FakePort(read_data=b"stale\n", eof=True)
    client = _attach(port)

    await client._write("FETCH?")

    assert port.read_data == b""
    assert port.writes == [b"FETCH?\n"]


async def test_local_setter_does_not_wait() -> None:
    port = _FakePort()
    client = _attach(port, settle=0)
    assert client._remote is False

    async with asyncio.timeout(0.1):
        await client.set_frequency(1_000)

    assert port.writes == [b"FREQ 1000\n"]


async def test_local_setter_sleeps_settle() -> None:
    port = _FakePort()
    client = _attach(port, settle=0.02)

    started = time.monotonic()
    await client.set_frequency(1_000)

    assert time.monotonic() - started >= 0.015
    assert port.writes == [b"FREQ 1000\n"]


async def test_remote_setter_waits_for_ack() -> None:
    port = _FakePort(response=b"exec success\n")
    client = _attach(port)
    client._remote = True

    await client.set_frequency(1_000)

    assert port.writes == [b"FREQ 1000\n"]
    assert port.read_data == b""


async def test_remote_setter_does_not_sleep() -> None:
    port = _FakePort(response=b"exec success\n")
    client = _attach(port, settle=0.2)
    client._remote = True

    started = time.monotonic()
    await client.set_frequency(1_000)

    assert time.monotonic() - started < 0.1
    assert port.writes == [b"FREQ 1000\n"]


async def test_remote_setter_raises_on_exec_err() -> None:
    port = _FakePort(response=b"exec err\n")
    client = _attach(port)
    client._remote = True

    with pytest.raises(RuntimeError, match="err$"):
        await client.set_frequency(1_000)


async def test_local_setter_does_not_check_errors() -> None:
    port = _FakePort(response=b"exec err\n")
    client = _attach(port, settle=0)
    assert client._remote is False

    await client.set_frequency(1_000)

    assert port.writes == [b"FREQ 1000\n"]
    assert port.read_data == b"exec err\n"


@pytest.mark.parametrize("reply", [b"cmd err\n", b"exec err\n", b"execu err\n", b"cmd err"])
async def test_read_raises_on_protocol_error(reply: bytes) -> None:
    client = _attach(_FakePort(read_data=reply), timeout=0.01)

    with pytest.raises(RuntimeError, match="err$"):
        await client._read()


async def test_command_rejects_unexpected_response() -> None:
    client = _attach(_FakePort(response=b"something\n"))

    with pytest.raises(RuntimeError):
        await client._command("SYST:REMOTE")


async def test_idn_rejects_malformed_response() -> None:
    client = _client("ignored", {"*IDN?": "ZC,ET433"})

    with pytest.raises(ValueError):
        await client.idn()


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("C", PrimaryMode.C),
        ("Cp", PrimaryMode.C),
        (" auto ", PrimaryMode.AUTO),
        ("L", PrimaryMode.L),
        ("DCR", PrimaryMode.DCR),
    ],
)
async def test_mode_property_parses_device_answer(answer: str, expected: PrimaryMode) -> None:
    assert await _client("ignored", {"FUNC:IMP:A?": answer}).get_mode() is expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("X", SecondaryMode.X),
        ("thr", SecondaryMode.THR),
        ("ESR", SecondaryMode.ESR),
        (" D ", SecondaryMode.D),
    ],
)
async def test_secondary_property_parses_device_answer(answer: str, expected: SecondaryMode) -> None:
    assert await _client("ignored", {"FUNC:IMP:B?": answer}).get_secondary() is expected


async def test_numeric_properties_return_float() -> None:
    client = _client(
        "ignored",
        {
            "FREQ?": "1.0E+03",
            "VOLT?": "600.0",
            "BIAS:VOLT?": "0.0",
        },
    )

    assert await client.get_frequency() == 1000.0
    assert await client.get_level_mv() == 600.0
    assert await client.get_bias_mv() == 0.0


async def test_numeric_setters_keep_fractional_values() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    await client.set_frequency(1000.5)
    await client.set_level_mv(600.0)
    await client.set_bias_mv(0.5)

    assert commands == ["FREQ 1000.5", "VOLT 600", "BIAS:VOLT 0.5"]


async def test_frequency_setter_sends_value_without_local_validation() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    await client.set_frequency(1)

    assert commands == ["FREQ 1"]


async def test_level_setter_sends_value_without_local_validation() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    await client.set_level_mv(5_000)

    assert commands == ["VOLT 5000"]


async def test_bias_setter_sends_value_without_local_validation() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    await client.set_bias_mv(2_000)

    assert commands == ["BIAS:VOLT 2000"]


async def test_display_page_property_and_command() -> None:
    client = _client("ignored", {"DISP:PAGE?": "SYSTem"})
    assert await client.get_display_page() is DisplayPage.SYST

    commands: list[str] = []
    _record_commands(client, commands)
    await client.set_display_page(DisplayPage.MEAS)
    await client.set_display_page(DisplayPage.SYST)
    assert commands == ["DISP:PAGE MEAS", "DISP:PAGE SYST"]


async def test_measurement_range_property_and_setter() -> None:
    assert await _client("ignored", {"FUNC:IMP:RANGE?": "1000"}).get_measurement_range() == 1000
    assert await _client("ignored", {"FUNC:IMP:RANGE?": "AUTO"}).get_measurement_range() == 0

    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)
    await client.set_measurement_range(100_000)
    await client.set_measurement_range(0)

    assert commands == [
        "FUNC:IMP:RANGE 100000",
        "FUNC:IMP:RANGE AUTO",
    ]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("ON", True),
        ("OFF", False),
        ("MAYBE", False),
    ],
)
async def test_relative_mode_property(answer: str, expected: bool) -> None:
    assert await _client("ignored", {"FUNC:DEV:MODE?": answer}).get_relative_mode() is expected


async def test_relative_mode_setter() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    await client.set_relative_mode(True)
    await client.set_relative_mode(False)

    assert commands == ["FUNC:DEV:MODE ON", "FUNC:DEV:MODE OFF"]


async def test_configure_applies_only_provided() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    await client.configure(frequency=500, delay=0)

    assert commands == ["FREQ 500"]


async def test_configure_all_parameters() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    await client.configure(
        mode=PrimaryMode.C,
        secondary=SecondaryMode.ESR,
        equivalent=Equivalent.SER,
        frequency=1000,
        level_mv=500,
        bias_mv=10,
        speed=Speed.FAST,
        measurement_range=1000,
        relative_mode=True,
        delay=0,
    )

    assert commands == [
        "FUNC:IMP:A C",
        "FUNC:IMP:B ESR",
        "FUNC:IMP:EQU SER",
        "FREQ 1000",
        "VOLT 500",
        "BIAS:VOLT 10",
        "APER FAST",
        "FUNC:IMP:RANGE 1000",
        "FUNC:DEV:MODE ON",
    ]


async def test_configure_measurement_range_auto() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    await client.configure(measurement_range=0, delay=0)

    assert commands == ["FUNC:IMP:RANGE AUTO"]


async def test_configure_relative_mode_false() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    await client.configure(relative_mode=False, delay=0)

    assert commands == ["FUNC:DEV:MODE OFF"]


async def test_configure_no_args() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    await client.configure(delay=0)

    assert commands == []


async def test_configure_delay_sleeps() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    started = time.monotonic()
    await client.configure(frequency=500, delay=0.02)

    assert time.monotonic() - started >= 0.015
    assert commands == ["FREQ 500"]


async def test_configure_delay_zero_does_not_sleep() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    async with asyncio.timeout(0.01):
        await client.configure(frequency=500, delay=0)

    assert commands == ["FREQ 500"]


async def test_configure_local_mode() -> None:
    port = _FakePort()
    client = _attach(port, settle=0)
    assert client._remote is False

    await client.configure(frequency=500, delay=0)

    assert port.writes == [b"FREQ 500\n"]


async def test_configure_remote_raises_exec_err() -> None:
    port = _FakePort(response=b"exec err\n")
    client = _attach(port, settle=0)
    client._remote = True

    with pytest.raises(RuntimeError, match="err$"):
        await client.configure(level_mv=1101, delay=0)


async def test_fetch_two_values() -> None:
    assert await _client("1e-6,2.5").fetch() == pytest.approx((1e-6, 2.5))


async def test_fetch_single_value_for_dcr() -> None:
    primary, secondary = await _client("5.8e9").fetch()
    assert primary == pytest.approx(5.8e9)
    assert secondary is None


async def test_fetch_returns_named_reading() -> None:
    reading = await _client("1e-6,2.5").fetch()
    assert isinstance(reading, Reading)
    assert reading.primary == pytest.approx(1e-6)
    assert reading.secondary == pytest.approx(2.5)


async def test_fetch_makes_only_one_request() -> None:
    calls: list[str] = []
    client = _client("1,2", calls=calls)

    assert await client.fetch() == pytest.approx((1.0, 2.0))
    assert calls == ["FETCH?"]


async def test_fetch_rejects_bad_answer() -> None:
    with pytest.raises(ValueError):
        await _client("garbage").fetch()


async def test_fetch_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        await _client("Cp,1e-6,D,0.01").fetch()
