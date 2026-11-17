#!/usr/bin/env python3
"""Basic ET43 usage example."""

import asyncio

from et43 import ET43, Equivalent, PrimaryMode, SecondaryMode

SETTLE = 2


async def main() -> None:
    async with (await ET43.open()).auto_remote() as lcr:
        print(await lcr.idn())
        print()

        # Capacitance at a few frequencies, series model, ESR as second parameter.
        await lcr.set_mode(PrimaryMode.C)
        await lcr.set_secondary(SecondaryMode.ESR)
        await lcr.set_level_mv(1_000)
        await lcr.set_equivalent(Equivalent.SER)

        for frequency in (10, 100, 1_000, 10_000, 100_000):
            await lcr.set_frequency(frequency)
            await asyncio.sleep(SETTLE)
            primary, secondary = await lcr.fetch()
            secondary_text = "—" if secondary is None else f"{secondary:.6g}"
            print(f"{frequency:>7} Hz   C = {primary:.6g} F, ESR = {secondary_text} Ω")

        print()

        # Configure once, then take several readings.
        await lcr.set_mode(PrimaryMode.L)
        await lcr.set_secondary(SecondaryMode.Q)
        await lcr.set_frequency(10_000)
        await lcr.set_level_mv(600)
        await lcr.set_equivalent(Equivalent.SER)
        await asyncio.sleep(SETTLE)
        for _ in range(5):
            primary, secondary = await lcr.fetch()
            secondary_text = "—" if secondary is None else f"{secondary:.6g}"
            print(f"L = {primary:.6g} H, Q = {secondary_text}")
            await asyncio.sleep(1.0)


if __name__ == "__main__":
    asyncio.run(main())
