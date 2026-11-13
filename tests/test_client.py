from typing import cast

import pytest
import serial

from et43.client import CALIBRATION_TIMEOUT, ET43, Reading
from et43.enums import (
    DisplayPage,
    Equivalent,
    PrimaryMode,
    SecondaryMode,
    Speed,
)


class _FakePort:
    """Minimal stand-in for ``serial.Serial`` in offline tests."""

    def __init__(self, is_open: bool = True, read_data: bytes = b"") -> None:
        self.is_open = is_open
        self.timeout = 1.0
        self.port = "/dev/fake"
        self.baudrate = 115_200
        self.writes: list[bytes] = []
        self.read_data = read_data
        self.read_timeouts: list[float | None] = []

    def reset_input_buffer(self) -> None:
        if not self.is_open:
            raise OSError("port is closed")

    def write(self, data: bytes) -> None:
        if not self.is_open:
            raise OSError("port is closed")
        self.writes.append(data)

    def flush(self) -> None:
        pass

    @property
    def in_waiting(self) -> int:
        return len(self.read_data)

    def read(self, size: int = 1) -> bytes:
        self.read_timeouts.append(self.timeout)
        data, self.read_data = self.read_data[:size], self.read_data[size:]
        return data

    def close(self) -> None:
        self.is_open = False


def _attach(port: _FakePort) -> ET43:
    """Create a client that owns *port*."""
    return ET43(cast(serial.Serial, port))


def _record_commands(client: ET43, commands: list[str]) -> None:
    def command(command: str, *, timeout: float | None = None) -> None:
        commands.append(command)

    client._command = command


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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("meas", DisplayPage.MEAS),
        ("SYST", DisplayPage.SYST),
        ("SYSTem", DisplayPage.SYST),
        ("SYSTEM", DisplayPage.SYST),
        ("MEASurement", DisplayPage.MEAS),
        (DisplayPage.SYST, DisplayPage.SYST),
    ],
)
def test_display_page_from_string(value: str | DisplayPage, expected: DisplayPage) -> None:
    assert DisplayPage.from_string(value) == expected


@pytest.mark.parametrize("value", ["SYS", "OTHER"])
def test_display_page_rejects_unknown(value: str) -> None:
    with pytest.raises(ValueError):
        DisplayPage.from_string(value)


def test_discover_without_ports_raises_connection_error() -> None:
    with pytest.raises(ConnectionError):
        ET43.discover(ports=[])


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

    def request(command: str, *, timeout: float | None = None) -> str:
        if calls is not None:
            calls.append(command)
        return mapping[command]

    client._request = request
    return client


def test_closed_port_state() -> None:
    client = _attach(_FakePort(is_open=False))

    assert client.is_open is False
    client.close()  # must be idempotent when the port is already closed

    with pytest.raises(OSError):
        client._request("FREQ?")


def test_constructor_performs_no_io() -> None:
    fake_port = _FakePort()
    client = ET43(cast(serial.Serial, fake_port))

    assert client.is_open is True
    client.close()
    assert fake_port.is_open is False


def test_idn_is_queried_each_time() -> None:
    calls: list[str] = []
    client = _attach(_FakePort())

    def request(command: str, *, timeout: float | None = None) -> str:
        calls.append(command)
        return "ZC,ET433,V1,H1,SN"

    client._request = request

    assert client.idn == client.idn
    assert calls == ["*IDN?", "*IDN?"]


def test_close_owns_port() -> None:
    fake_port = _FakePort()
    client = _attach(fake_port)

    client.close()
    assert fake_port.writes == []
    assert fake_port.is_open is False


def test_write_does_not_read() -> None:
    fake_port = _FakePort()
    client = _attach(fake_port)

    client._write("SYST:LOCAL")
    assert fake_port.writes == [b"SYST:LOCAL\n"]


def test_remote_sends_command_and_waits_for_ack() -> None:
    fake_port = _FakePort(read_data=b"exec success\n")
    client = _attach(fake_port)

    client.remote()
    assert fake_port.writes == [b"SYST:REMOTE\n"]


def test_equivalent_par_sends_firmware_token() -> None:
    fake_port = _FakePort(read_data=b"exec success\n")
    client = _attach(fake_port)

    client.equivalent = Equivalent.PAR
    assert fake_port.writes == [b"FUNC:IMP:EQU PAL\n"]


def test_equivalent_parses_pallel_response() -> None:
    fake_port = _FakePort(read_data=b"PALlel\n")
    client = _attach(fake_port)

    assert client.equivalent is Equivalent.PAR


def test_local_writes_without_reading_ack() -> None:
    fake_port = _FakePort()
    client = _attach(fake_port)

    client.local()
    assert fake_port.writes == [b"SYST:LOCAL\n"]


def test_auto_remote_enters_and_exits() -> None:
    port = _FakePort(read_data=b"exec success\n")
    client = _attach(port)

    with client.auto_remote() as current:
        assert current is client
        assert port.writes == [b"SYST:REMOTE\n"]
        assert port.is_open

    assert port.writes == [b"SYST:REMOTE\n", b"SYST:LOCAL\n"]
    assert not port.is_open


def test_auto_remote_exits_on_exception() -> None:
    port = _FakePort(read_data=b"exec success\n")
    client = _attach(port)

    with pytest.raises(RuntimeError), client.auto_remote():
        raise RuntimeError("boom")

    assert port.writes == [b"SYST:REMOTE\n", b"SYST:LOCAL\n"]
    assert not port.is_open


def test_auto_remote_exits_when_remote_fails() -> None:
    port = _FakePort()  # no acknowledgement -> remote() times out
    client = _attach(port)

    with pytest.raises(TimeoutError), client.auto_remote():
        pytest.fail("block must not run")

    assert port.writes == [b"SYST:REMOTE\n", b"SYST:LOCAL\n"]
    assert not port.is_open


def test_auto_remote_local_failure_does_not_mask_block_error() -> None:
    port = _FakePort(read_data=b"exec success\n")
    client = _attach(port)

    def broken_local() -> None:
        raise OSError("port gone")

    client.local = broken_local

    with pytest.raises(RuntimeError, match="boom"), client.auto_remote():
        raise RuntimeError("boom")

    assert not port.is_open


def test_auto_remote_local_failure_after_success_propagates() -> None:
    port = _FakePort(read_data=b"exec success\n")
    client = _attach(port)

    def broken_local() -> None:
        raise OSError("port gone")

    client.local = broken_local

    with pytest.raises(OSError, match="port gone"), client.auto_remote():
        pass

    assert not port.is_open


def test_calibrate_waits_for_ack() -> None:
    fake_port = _FakePort(read_data=b"exec success\n")
    client = _attach(fake_port)

    client.calibrate()
    assert fake_port.writes == [b"CORR:EXEC\n"]


def test_calibrate_uses_calibration_timeout() -> None:
    client = _attach(_FakePort())
    seen: list[float | None] = []

    def fake_read(timeout: float | None = None) -> str:
        seen.append(timeout)
        return "exec success"

    client._read = fake_read  # type: ignore[method-assign]
    client.calibrate()

    assert seen == [CALIBRATION_TIMEOUT]


def test_calibrate_timeout_override() -> None:
    client = _attach(_FakePort())
    seen: list[float | None] = []

    def fake_read(timeout: float | None = None) -> str:
        seen.append(timeout)
        return "exec success"

    client._read = fake_read  # type: ignore[method-assign]
    client.calibrate(timeout=12.5)

    assert seen == [12.5]


def test_read_consumes_crlf_terminator() -> None:
    port = _FakePort(read_data=b"1,2\r\n")
    client = _attach(port)

    assert client._read() == "1,2"
    assert port.read_data == b""


def test_read_accepts_bare_cr_and_lf() -> None:
    assert _attach(_FakePort(read_data=b"1,2\r"))._read() == "1,2"
    assert _attach(_FakePort(read_data=b"1,2\n"))._read() == "1,2"


def test_read_keeps_next_response_after_crlf() -> None:
    port = _FakePort(read_data=b"1,2\r\n3,4\n")
    client = _attach(port)

    assert client._read() == "1,2"
    assert client._read() == "3,4"


def test_read_preserves_byte_after_bare_cr() -> None:
    port = _FakePort(read_data=b"1,2\r3,4\n")
    client = _attach(port)

    assert client._read() == "1,2"
    assert client._read() == "3,4"


def test_read_applies_explicit_timeout_and_restores_it() -> None:
    port = _FakePort(read_data=b"exec success\n")
    client = _attach(port)

    assert client._read(timeout=12.5) == "exec success"
    assert set(port.read_timeouts) == {12.5}
    assert port.timeout == 1.0


def test_read_raises_on_empty_response() -> None:
    client = _attach(_FakePort(read_data=b""))

    with pytest.raises(TimeoutError):
        client._read()


@pytest.mark.parametrize("reply", [b"cmd err\n", b"exec err\n", b"execu err\n", b"cmd err"])
def test_read_raises_on_protocol_error(reply: bytes) -> None:
    client = _attach(_FakePort(read_data=reply))

    with pytest.raises(RuntimeError, match="err$"):
        client._read()


def test_command_rejects_unexpected_response() -> None:
    client = _attach(_FakePort(read_data=b"something\n"))

    with pytest.raises(RuntimeError):
        client._command("SYST:REMOTE")


def test_idn_rejects_malformed_response() -> None:
    client = _client("ignored", {"*IDN?": "ZC,ET433"})

    with pytest.raises(ValueError):
        _ = client.idn


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
def test_mode_property_parses_device_answer(answer: str, expected: PrimaryMode) -> None:
    assert _client("ignored", {"FUNC:IMP:A?": answer}).mode is expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("X", SecondaryMode.X),
        ("thr", SecondaryMode.THR),
        ("ESR", SecondaryMode.ESR),
        (" D ", SecondaryMode.D),
    ],
)
def test_secondary_property_parses_device_answer(answer: str, expected: SecondaryMode) -> None:
    assert _client("ignored", {"FUNC:IMP:B?": answer}).secondary is expected


def test_numeric_properties_return_float() -> None:
    client = _client(
        "ignored",
        {
            "FREQ?": "1.0E+03",
            "VOLT?": "600.0",
            "BIAS:VOLT?": "0.0",
        },
    )

    assert client.frequency == 1000.0
    assert client.level_mv == 600.0
    assert client.bias_mv == 0.0


def test_numeric_setters_keep_fractional_values() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    client.frequency = 1000.5
    client.level_mv = 600.0
    client.bias_mv = 0.5

    assert commands == ["FREQ 1000.5", "VOLT 600", "BIAS:VOLT 0.5"]


def test_frequency_setter_sends_value_without_local_validation() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    client.frequency = 1

    assert commands == ["FREQ 1"]


def test_level_setter_sends_value_without_local_validation() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    client.level_mv = 5_000

    assert commands == ["VOLT 5000"]


def test_bias_setter_sends_value_without_local_validation() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    client.bias_mv = 2_000

    assert commands == ["BIAS:VOLT 2000"]


def test_display_page_property_and_command() -> None:
    client = _client("ignored", {"DISP:PAGE?": "SYSTem"})
    assert client.display_page is DisplayPage.SYST

    commands: list[str] = []
    _record_commands(client, commands)
    client.display_page = DisplayPage.MEAS
    client.display_page = DisplayPage.SYST
    assert commands == ["DISP:PAGE MEAS", "DISP:PAGE SYST"]


def test_measurement_range_property_and_setter() -> None:
    assert _client("ignored", {"FUNC:IMP:RANGE?": "1000"}).measurement_range == 1000
    assert _client("ignored", {"FUNC:IMP:RANGE?": "AUTO"}).measurement_range is None

    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)
    client.measurement_range = 100_000
    client.measurement_range = None

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
def test_relative_mode_property(answer: str, expected: bool) -> None:
    assert _client("ignored", {"FUNC:DEV:MODE?": answer}).relative_mode is expected


def test_relative_mode_setter() -> None:
    client = _client("ignored")
    commands: list[str] = []
    _record_commands(client, commands)

    client.relative_mode = True
    client.relative_mode = False

    assert commands == ["FUNC:DEV:MODE ON", "FUNC:DEV:MODE OFF"]


def test_fetch_two_values() -> None:
    assert _client("1e-6,2.5").fetch() == pytest.approx((1e-6, 2.5))


def test_fetch_single_value_for_dcr() -> None:
    primary, secondary = _client("5.8e9").fetch()
    assert primary == pytest.approx(5.8e9)
    assert secondary is None


def test_fetch_returns_named_reading() -> None:
    reading = _client("1e-6,2.5").fetch()
    assert isinstance(reading, Reading)
    assert reading.primary == pytest.approx(1e-6)
    assert reading.secondary == pytest.approx(2.5)


def test_fetch_makes_only_one_request() -> None:
    calls: list[str] = []
    client = _client("1,2", calls=calls)

    assert client.fetch() == pytest.approx((1.0, 2.0))
    assert calls == ["FETCH?"]


def test_fetch_rejects_bad_answer() -> None:
    with pytest.raises(ValueError):
        _client("garbage").fetch()


def test_fetch_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        _client("Cp,1e-6,D,0.01").fetch()
