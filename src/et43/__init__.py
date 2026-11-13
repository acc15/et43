"""Human-friendly Python client for EastTester ET43x LCR meters (ET430-ET433)."""

from importlib.metadata import version as _version

from .client import ET43, Idn, Reading
from .enums import (
    DisplayPage,
    Equivalent,
    PrimaryMode,
    SecondaryMode,
    Speed,
)

__all__ = [
    "ET43",
    "DisplayPage",
    "Equivalent",
    "Idn",
    "PrimaryMode",
    "Reading",
    "SecondaryMode",
    "Speed",
]

__version__ = _version("et43")
