#!/usr/bin/env python3
"""Basic ET43 usage example."""

import time

from et43 import ET43, Equivalent, PrimaryMode, SecondaryMode

SETTLE = 2


def main() -> None:
    with ET43.open().auto_remote() as lcr:
        print(lcr.idn)
        print()

        # Capacitance at a few frequencies, series model, ESR as second parameter.
        lcr.mode = PrimaryMode.C
        lcr.secondary = SecondaryMode.ESR
        lcr.level_mv = 1_000
        lcr.equivalent = Equivalent.SER

        for frequency in (10, 100, 1_000, 10_000, 100_000):
            lcr.frequency = frequency
            time.sleep(SETTLE)
            primary, secondary = lcr.fetch()
            secondary_text = "—" if secondary is None else f"{secondary:.6g}"
            print(f"{frequency:>7} Hz   C = {primary:.6g} F, ESR = {secondary_text} Ω")

        print()

        # Configure once, then take several readings.
        lcr.mode = PrimaryMode.L
        lcr.secondary = SecondaryMode.Q
        lcr.frequency = 10_000
        lcr.level_mv = 600
        lcr.equivalent = Equivalent.SER
        time.sleep(SETTLE)
        for _ in range(5):
            primary, secondary = lcr.fetch()
            secondary_text = "—" if secondary is None else f"{secondary:.6g}"
            print(f"L = {primary:.6g} H, Q = {secondary_text}")
            time.sleep(1.0)


if __name__ == "__main__":
    main()
