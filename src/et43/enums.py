"""Strongly typed instrument settings."""
from enum import StrEnum
from typing import Self


class ParsedUnit(StrEnum):
    """Base for SCPI enums that can be parsed from short or long tokens."""

    @classmethod
    def _matches(cls, text: str, member: Self) -> bool:
        """Whether *text* names *member*; subclasses may tighten this."""
        return text == member.name or text.startswith(member.value)

    @classmethod
    def from_string(cls, value: str | int | Self) -> Self:
        """Match a short, long, or exact SCPI token to a member."""
        if isinstance(value, cls):
            return value

        text = str(value).strip().upper()
        for member in cls:
            if cls._matches(text, member):
                return member

        choices = ", ".join(member.value for member in cls)
        raise ValueError(f"{cls.__name__} must be one of {choices}")


class SecondaryMode(ParsedUnit):
    """Secondary measurement parameter."""

    X = "X"
    D = "D"
    Q = "Q"
    THR = "THR"
    ESR = "ESR"


class PrimaryMode(ParsedUnit):
    """Primary measurement parameter."""

    AUTO = "AUTO"
    R = "R"
    C = "C"
    L = "L"
    Z = "Z"
    DCR = "DCR"
    ECAP = "ECAP"


class Speed(ParsedUnit):
    """Measurement speed."""

    FAST = "FAST"
    MED = "MED"
    SLOW = "SLOW"


class Equivalent(ParsedUnit):
    """Series or parallel equivalent circuit.

    EastTester firmware (ET43x, ET44x/ET45x) spells parallel as ``PALlel``:
    it rejects ``PAR`` and reports ``PALlel``. The member is named ``PAR``,
    but its value is the token the instrument actually accepts.
    """

    SER = "SER"
    # "PAL" is not a typo here — it is EastTester's typo. Their SCPI manual
    # (ET44/ET45, shared firmware lineage with ET43x) defines the keyword as
    # "PALlel" (sic, a misspelling of "PARallel"). By SCPI rules the short form
    # is the uppercase prefix, hence "PAL". The firmware enforces it: verified on
    # a real ET433 (V2.00.2414.001), "FUNC:IMP:EQU PAR" is rejected and
    # "FUNC:IMP:EQU?" answers "PALlel". Other vendors use their own syntax
    # (e.g. Keysight encodes series/parallel in FUNC:IMP:TYPE CPD/CSRS), so we
    # just match EastTester. The member is still called PAR for readability.
    PAR = "PAL"


class DisplayPage(ParsedUnit):
    """Front-panel display page as its SCPI token."""

    MEAS = "MEAS"
    SYST = "SYST"
