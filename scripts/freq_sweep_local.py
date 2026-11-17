import asyncio

from et43 import ET43, Equivalent, PrimaryMode, SecondaryMode, Speed


async def main() -> None:
    async with (await ET43.open()).auto_remote() as lcr:
        print(await lcr.idn())
        await lcr.configure(
            PrimaryMode.C, 
            SecondaryMode.ESR, 
            Equivalent.SER, 
            level_mv=1000, 
            bias_mv=0, 
            speed=Speed.SLOW,
            measurement_range=0, 
            relative_mode=False,
            delay=0
        )
        for frequency in (10, 100, 1_000, 10_000, 100_000):
            print(f"{frequency:>7}", end = " ")
            await lcr.configure(frequency = frequency)
            primary, secondary = await lcr.fetch()
            secondary_text = "—" if secondary is None else f"{secondary:.6g}"
            print(f"C = {primary:.6g} F, ESR = {secondary_text} Ω")

if __name__ == "__main__":
    asyncio.run(main())
