#!/usr/bin/env python3
"""Frequency sweep with CSV output."""

import asyncio
import csv
import math
from pathlib import Path

from et43 import ET43, Equivalent, PrimaryMode, SecondaryMode

SETTLE = 0.5


def frequency_points(start: int, stop: int, points: int, *, logarithmic: bool) -> list[int]:
    """Evenly spaced (or logarithmic) frequencies, deduplicated to 1 Hz."""
    if points < 2:
        raise ValueError("a sweep needs at least 2 points")
    if start == stop:
        return [start]

    if logarithmic:
        low, high = math.log(start), math.log(stop)
        raw = [math.exp(low + (high - low) * i / (points - 1)) for i in range(points)]
    else:
        raw = [start + (stop - start) * i / (points - 1) for i in range(points)]

    values = [round(value) for value in raw]
    values[0], values[-1] = start, stop

    result: list[int] = []
    for value in values:
        if not result or value != result[-1]:
            result.append(value)
    return result


async def main() -> None:
    output = Path("sweep.csv")
    frequencies = frequency_points(100, 100_000, 25, logarithmic=True)

    async with (await ET43.open()).auto_remote() as lcr:
        await lcr.set_mode(PrimaryMode.C)
        await lcr.set_secondary(SecondaryMode.ESR)
        await lcr.set_level_mv(1_000)
        await lcr.set_equivalent(Equivalent.SER)

        # Local CSV I/O is tiny; keep the example simple.
        with output.open("w", newline="", encoding="utf-8") as handle:  # noqa: ASYNC230
            writer = csv.DictWriter(handle, fieldnames=["frequency_hz", "primary", "secondary"])
            writer.writeheader()

            for frequency in frequencies:
                await lcr.set_frequency(frequency)
                await asyncio.sleep(SETTLE)
                primary, secondary = await lcr.fetch()
                secondary_text = "—" if secondary is None else f"{secondary:.6g}"
                print(f"{frequency:>7} Hz   {primary:.6g}   {secondary_text}")
                writer.writerow(
                    {
                        "frequency_hz": frequency,
                        "primary": primary,
                        "secondary": secondary,
                    }
                )

    print(f"saved {output}")


if __name__ == "__main__":
    asyncio.run(main())
