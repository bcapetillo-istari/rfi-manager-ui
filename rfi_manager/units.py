"""Deterministic unit conversion for T/O grading (docs/T-O_COMPLIANCE.md).

A ``{family: {canonical_unit: factor}}`` table plus an alias-normalization
layer — the hard part is aliases ("KTAS", "hrs", "ft MSL"), not arithmetic.
Stdlib only by design (no pint): grading needs five unit families, not
generality. Unknown or cross-family conversions raise UnsupportedConversion —
the grader fails fast to NOT_GRADEABLE, never guesses.

Factors convert a value INTO the family's base unit (SI-ish base per family);
converting a→b is value * factor[a] / factor[b].
"""

from __future__ import annotations

import re


class UnsupportedConversion(Exception):
    """Raised when a unit is unknown or the units are in different families."""


# family -> canonical unit -> factor to the family's base unit
_FACTORS: dict[str, dict[str, float]] = {
    "length": {  # base: meter
        "m": 1.0,
        "km": 1000.0,
        "mi": 1609.344,
        "nmi": 1852.0,
        "ft": 0.3048,
        "yd": 0.9144,
        "in": 0.0254,
        "cm": 0.01,
        "mm": 0.001,
    },
    "time": {  # base: second
        "s": 1.0,
        "min": 60.0,
        "hr": 3600.0,
        "day": 86400.0,
        "week": 604800.0,
        "month": 2629800.0,  # mean Gregorian month (365.25/12 days)
        "year": 31557600.0,
    },
    "speed": {  # base: meter/second
        "m/s": 1.0,
        "km/h": 1000.0 / 3600.0,
        "mph": 1609.344 / 3600.0,
        "kt": 1852.0 / 3600.0,
        "ft/s": 0.3048,
        "ft/min": 0.3048 / 60.0,
    },
    "mass": {  # base: kilogram
        "kg": 1.0,
        "g": 0.001,
        "lb": 0.45359237,
        "t": 1000.0,
        "oz": 0.028349523125,
    },
    "data_rate": {  # base: bit/second (decimal prefixes per telecom convention)
        "bps": 1.0,
        "kbps": 1e3,
        "mbps": 1e6,
        "gbps": 1e9,
    },
}

# alias (lowercased, normalized) -> canonical unit. Aviation note: bare
# "miles" in this domain is ambiguous (statute vs nautical) — it is mapped to
# statute miles here; RFIs that mean nautical almost always write nm/nmi/knots.
_ALIASES: dict[str, str] = {
    # length
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "km": "km", "kilometer": "km", "kilometers": "km", "kilometre": "km",
    "kilometres": "km",
    "mi": "mi", "mile": "mi", "miles": "mi", "sm": "mi", "statute mile": "mi",
    "statute miles": "mi",
    "nmi": "nmi", "nm": "nmi", "nautical mile": "nmi", "nautical miles": "nmi",
    "ft": "ft", "foot": "ft", "feet": "ft", "ft msl": "ft", "ft agl": "ft",
    "yd": "yd", "yard": "yd", "yards": "yd",
    "in": "in", "inch": "in", "inches": "in",
    "cm": "cm", "centimeter": "cm", "centimeters": "cm",
    "mm": "mm", "millimeter": "mm", "millimeters": "mm",
    # time
    "s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "min": "min", "mins": "min", "minute": "min", "minutes": "min",
    "hr": "hr", "hrs": "hr", "h": "hr", "hour": "hr", "hours": "hr",
    "day": "day", "days": "day",
    "week": "week", "weeks": "week", "wk": "week",
    "month": "month", "months": "month", "mo": "month", "mos": "month",
    "year": "year", "years": "year", "yr": "year", "yrs": "year",
    # speed
    "m/s": "m/s", "mps": "m/s", "meters per second": "m/s",
    "km/h": "km/h", "kph": "km/h", "kmh": "km/h", "km/hr": "km/h",
    "kilometers per hour": "km/h",
    "mph": "mph", "mi/h": "mph", "miles per hour": "mph",
    "kt": "kt", "kts": "kt", "knot": "kt", "knots": "kt",
    "ktas": "kt", "kias": "kt", "kcas": "kt",  # airspeed variants; grading
    # treats them as knots — the TAS/IAS distinction is beyond v1 scope
    "ft/s": "ft/s", "fps": "ft/s", "feet per second": "ft/s",
    "ft/min": "ft/min", "fpm": "ft/min", "feet per minute": "ft/min",
    # mass
    "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg",
    "g": "g", "gram": "g", "grams": "g",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "t": "t", "tonne": "t", "tonnes": "t", "metric ton": "t", "metric tons": "t",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    # data rate
    "bps": "bps", "bit/s": "bps", "bits per second": "bps",
    "kbps": "kbps", "kbit/s": "kbps",
    "mbps": "mbps", "mbit/s": "mbps", "megabits per second": "mbps",
    "gbps": "gbps", "gbit/s": "gbps", "gigabits per second": "gbps",
}

_CANONICAL_FAMILY: dict[str, str] = {
    canonical: family
    for family, table in _FACTORS.items()
    for canonical in table
}

_WS_RE = re.compile(r"\s+")


def normalize_unit(unit: str) -> str:
    """Resolve a raw unit string ("KTAS", " Hrs ", "ft MSL") to its canonical
    unit ("kt", "hr", "ft"). Raises UnsupportedConversion when unknown."""
    key = _WS_RE.sub(" ", unit.strip().lower())
    # data-rate casing like "Mbps" lowercases cleanly; nothing else special
    if key not in _ALIASES:
        raise UnsupportedConversion(f"unknown unit: {unit!r}")
    return _ALIASES[key]


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert ``value`` between two units of the same family. Raises
    UnsupportedConversion for unknown units or cross-family conversions
    (e.g. kg -> km)."""
    src = normalize_unit(from_unit)
    dst = normalize_unit(to_unit)
    src_family = _CANONICAL_FAMILY[src]
    dst_family = _CANONICAL_FAMILY[dst]
    if src_family != dst_family:
        raise UnsupportedConversion(
            f"cannot convert {from_unit!r} ({src_family}) to {to_unit!r} ({dst_family})"
        )
    table = _FACTORS[src_family]
    return value * table[src] / table[dst]
