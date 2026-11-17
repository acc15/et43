# et43

A human-friendly Python client for **EastTester ET43x** handheld LCR meters
(ET430/430B/431/432/**ET433**) over their USB virtual serial port.

> Unofficial, not affiliated with EastTester / Hangzhou Zhongchuang.

Instead of raw SCPI strings you get typed methods: `set_mode()`,
`set_frequency()`, `set_level_mv()`, `set_equivalent()`, `fetch()` and so on.

- Async API built on [`asyncio`](https://docs.python.org/3/library/asyncio.html)
  and [`serialx`](https://pypi.org/project/serialx/) (no PyVISA).
- Fully typed (`py.typed`).
- Works with plain devices as well as serialx URLs (`socket://`,
  `rfc2217://`, `esphome://`).
- Tested against a real ET433 (firmware `V2.00.2414.001`); see
  [Firmware quirks](#firmware-quirks).

### ET44x / ET45x

The benchtop ET44x/ET45x series speaks a very similar SCPI dialect, so most of
the client may work there too, but it has not been tested. Known differences,
based on the vendor's ET44/ET45 SCPI manual and the
[ET44.py](https://github.com/philpagel/ET44.py) driver:

- auto range is `FUNC:IMP:RANGe:AUTO ON|OFF` there, while the ET433 needs
  `FUNC:IMP:RANGE AUTO`, so `get_measurement_range()` will likely fail;
- ET44.py sends `THETA` for phase, which the ET433 rejects (it needs `THR`);
- the default baud rate is 9600 (`ET43.discover()` tries it as well).

Reports from ET44x/ET45x owners are welcome.

## Installation

```bash
pip install et43
# or
uv add et43
```

Requires Python 3.14+.

## Quick start

```python
import asyncio

from et43 import ET43, Equivalent, PrimaryMode, SecondaryMode


async def main() -> None:
    async with (await ET43.open("/dev/ttyUSB0")).auto_remote() as lcr:
        print(await lcr.idn())

        # Capacitance and ESR at 1 kHz, 1 V test level, series model
        await lcr.set_mode(PrimaryMode.C)
        await lcr.set_secondary(SecondaryMode.ESR)
        await lcr.set_frequency(1_000)
        await lcr.set_level_mv(1_000)
        await lcr.set_equivalent(Equivalent.SER)

        primary, secondary = await lcr.fetch()
        print(primary, secondary)  # 1.234e-06 0.5


asyncio.run(main())
```

Without a port name the client looks for the instrument itself:

```python
lcr = await ET43.open()  # auto-discovery, tries 115200 and 9600 baud
```

Discovery probes only USB serial adapters (the ET433 uses a CH340) and never
touches other ports such as built-in `/dev/ttyS*`. If no adapter is found it
raises `ConnectionError` right away; pass the port name explicitly for other
setups.

`ET43.open()` is an async convenience factory: it creates and configures a
`serialx.AsyncSerial` via `serialx.async_serial_for_url()` and hands it to the
client. If you already have a port object, pass it directly:

```python
import serialx

from et43 import ET43

link = serialx.async_serial_for_url("/dev/ttyUSB0", baudrate=115_200)
await link.open()

async with ET43(link, timeout=2.0).auto_remote() as lcr:
    primary, secondary = await lcr.fetch()
```

`port` may also be a serialx URL, for example
`await ET43.open("rfc2217://192.168.1.10:4001")` or `socket://host:port`.

The client always owns the port it is given: `close()` and leaving an
`async with` block close it, even if you created the `serialx.AsyncSerial`
yourself. The response timeout is the client's `timeout` argument (2 seconds by
default); it is no longer read from the port. The port object is available as
`lcr.port`.

The `ET43(link)` constructor performs no I/O. The first exchange happens on
`await lcr.remote()`, `await lcr.fetch()` or another explicit call.

On POSIX the port is locked with `flock` by default, so a second process gets
`serialx.SerialException` (or `OSError`) instead of corrupting the exchange.
Disable this with `await ET43.open(..., exclusive=False)`.

I/O errors are raised as native `OSError`, `serialx.SerialException` or
`TimeoutError`, not as pyserial's `serial.SerialException`.

## Settings

Every setting can be read and written with an async getter and setter:

```python
from et43 import DisplayPage, Equivalent, PrimaryMode, SecondaryMode, Speed

await lcr.set_mode(PrimaryMode.C)
await lcr.set_secondary(SecondaryMode.ESR)
await lcr.set_frequency(10_000)  # Hz
await lcr.set_level_mv(600)  # mV
await lcr.set_bias_mv(100)  # mV
await lcr.set_equivalent(Equivalent.SER)
await lcr.set_speed(Speed.MED)
await lcr.set_measurement_range(None)  # None = AUTO, a number = range in ohms
await lcr.set_relative_mode(True)
await lcr.set_display_page(DisplayPage.SYST)

mode = await lcr.get_mode()  # PrimaryMode.C
frequency = await lcr.get_frequency()  # 10000.0
```

Enum getters and setters (`get_mode`/`set_mode`, `get_secondary`/`set_secondary`,
`get_equivalent`/`set_equivalent`, `get_speed`/`set_speed`,
`get_display_page`/`set_display_page`) accept enum members only, and the
getters return enum members. They are `StrEnum`s, so comparing with a string
still works: `await lcr.get_mode() == "C"`.

`get_measurement_range()` returns `int | None`: `None` means `AUTO`, otherwise
it is the range resistance in ohms.

`get_frequency()`, `get_level_mv()` and `get_bias_mv()` return `float`, because
SCPI returns them in NR3 format. In practice the ET433 works in 1 Hz / 1 mV
steps.

The client does not validate ranges: values are sent to the instrument as is.
Valid ranges depend on the model, and the instrument decides how to handle an
invalid value. For example, the ET433 silently ignores an out-of-range frequency
or bias, and answers `exec err` to an invalid level or measurement range.

Parsing helpers on the enums:

```python
DisplayPage.SYST  # "SYST", the SCPI token; the instrument answers "SYSTem"
PrimaryMode.from_string("c")  # PrimaryMode.C
PrimaryMode.from_string("Cp")  # PrimaryMode.C (older firmware answers)
Speed.from_string("MEDium")  # Speed.MED (instrument answer)
```

## Measurements

`fetch()` is a coroutine: `await lcr.fetch()` sends exactly one `FETCH?` query
and returns a `Reading`, a two-field `NamedTuple`. Unpack it or use the field
names:

```python
primary, secondary = await lcr.fetch()

reading = await lcr.fetch()
print(reading.primary, reading.secondary)
```

- `primary` is always a `float`.
- `secondary` is a `float` or `None`: in `DCR` mode, for example, the
  instrument returns a single value. The tuple always has two items, so
  `primary, secondary = ...` works in every mode.

`FETCH?` does not report the mode or units, so the client makes no extra
queries. Set the mode, the equivalent circuit and other parameters through the
setters:

```python
import asyncio

await lcr.set_mode(PrimaryMode.C)
await lcr.set_secondary(SecondaryMode.ESR)
await lcr.set_frequency(1_000)

await asyncio.sleep(0.5)  # let the instrument settle
primary, secondary = await lcr.fetch()
```

`fetch()` neither configures the instrument nor waits: it is a single query.
Measurement loops and frequency sweeps are left to your code; see
[`scripts/`](https://github.com/acc15/et43/tree/master/scripts) in the repository for examples.

## Remote and local mode

Neither `ET43.open()` nor `ET43(link)` switches the instrument to remote.
There are two ways to use the client:

- a plain `async with` block: the instrument stays in local mode;
- `auto_remote()`: remote mode for the duration of the block, a guaranteed
  return to local mode however the block ends, then the port is closed.

```python
lcr = await ET43.open("/dev/ttyUSB0")
async with lcr:
    print(await lcr.fetch())  # local mode

async with (await ET43.open("/dev/ttyUSB0")).auto_remote() as lcr:
    print(await lcr.fetch())  # remote mode; SYST:LOCAL and close() afterwards
```

`auto_remote()` sends `SYST:REMOTE` on entry and `SYST:LOCAL` on exit, also when
the block raises. This matters: in remote mode the front panel is locked, and it
must not stay locked after your program ends. If an error occurs while
returning to local mode after another error, the original error is the one you
see. Call it right on `await open()`, since it closes the port on exit. To keep
the connection open across blocks, call `await lcr.remote()` and
`await lcr.local()` yourself.

In local mode the instrument answers queries (`FETCH?`, `FREQ?` and so on) but
**ignores configuration commands**. The client reports this as an error rather
than pretending the setting was applied:

```python
lcr = await ET43.open("/dev/ttyUSB0")
async with lcr:
    print(await lcr.fetch())  # queries work
    await lcr.set_frequency(10_000)  # raises TimeoutError in local mode
```

## Calibration

```python
await lcr.calibrate()  # CORR:EXEC
```

The instrument detects what is connected, open or short, by itself. Connect open
leads and calibrate, then short the leads and calibrate again.
**This overwrites the stored open/short correction data.**

`calibrate()` waits up to 45 seconds for the `exec success` acknowledgement
(a real ET433 takes about 45 seconds). The timeout can be changed:

```python
await lcr.calibrate(timeout=60)
```

## Concurrency

Requests are serialized with an `asyncio.Lock`, so several tasks can share one
client: a request/response pair never interleaves with another. A sequence of
separate commands is not atomic; wrap it in your own `asyncio.Lock` if other
tasks use the same client.

The client is bound to the event loop it was opened in. Do not pass it between
threads or event loops; create one client per loop.

## Firmware quirks

Verified on a real ET433 with firmware `V2.00.2414.001`:

- **Frequency:** 10–100000 Hz. 10 Hz really works and gives correct C/ESR on a
  2.2 µF electrolytic capacitor. Out-of-range values are ignored: the instrument
  answers `exec success` but keeps the old frequency. The ET432 range differs.
- **Bias:** 0–1000 mV. The older PDF says 0–500 mV, but the V2 manual and the
  instrument allow 0–1000 mV. Out-of-range values are ignored, fractional
  values are truncated to whole millivolts.
- **ECAP mode** enables an internal 1 V bias, which may stay on after switching
  to another mode. Check `await lcr.get_bias_mv()` if it matters.
- **Level:** 0–1100 mV in whole millivolts. `1101`, negative and fractional
  values (including `0.001` mV) are rejected with `exec err`.
- **Baud rate:** 115200. The manual mentions 9600, so `ET43.discover()` tries both.
- **`FETCH?`** returns **two** comma-separated values (`primary,secondary`),
  while the older SCPI PDF describes four. In `DCR` mode it returns **one**
  value, and `Reading.secondary` is `None`. Mode and units are not included.
- **Line terminators** vary between responses (`\n` and `\r`); the reader
  accepts both.
- **Parallel equivalent circuit** is spelled `PAL`: `FUNC:IMP:EQU PAR` is
  rejected, and `FUNC:IMP:EQU?` answers `SERial` or `PALlel`. This is an
  EastTester misspelling of "parallel", shared by the ET44x/ET45x and present
  in their SCPI manual. The API member is `Equivalent.PAR`, and its value is
  `"PAL"`, the token the instrument accepts. `from_string` understands `PAR`,
  `PAL` and `PALlel`.
- **Phase:** `FUNC:IMP:B THETA` is rejected; use `THR`. Phase is returned in
  **degrees**.
- **Auto range:** `FUNC:IMP:RANGE:AUTO` does not work; the correct form is
  `FUNC:IMP:RANGE AUTO`.
- **`AUTO` mode** does not report which parameter was actually measured, and
  `fetch()` returns bare numbers. Set the mode explicitly (`C`, `L`, `R`, `Z`)
  if you need units and meaningful labels.

## Development

The project uses [uv](https://docs.astral.sh/uv/) and
[just](https://github.com/casey/just).

```bash
uv sync       # create .venv with the package and dev tools
just check    # ty + ruff + pytest
just build    # check, then build sdist and wheel into dist/
```

The tests do not need an instrument. The example scripts do:

```bash
uv run scripts/basic.py
uv run scripts/sweep_csv.py
```

Project layout:

```
src/et43/
  client.py     # ET43: transport, settings, fetch, remote/local
  enums.py      # PrimaryMode, SecondaryMode, Speed, Equivalent, DisplayPage
scripts/        # usage examples (not part of the package)
tests/
```

## License

[MIT](https://github.com/acc15/et43/blob/master/LICENSE)
