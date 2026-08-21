"""units.py: conversion arithmetic, alias normalization, fail-fast on
unknown/cross-family conversions (docs/T-O_VALIDATION.md)."""

from __future__ import annotations

import pytest

from rfi_manager.units import UnsupportedConversion, convert, normalize_unit


@pytest.mark.parametrize("raw,canonical", [
    ("km", "km"),
    ("Kilometers", "km"),
    ("nm", "nmi"),
    ("nautical miles", "nmi"),
    ("KTAS", "kt"),
    ("kts", "kt"),
    ("knots", "kt"),
    ("Hrs", "hr"),
    ("  hours ", "hr"),
    ("ft MSL", "ft"),
    ("Mbps", "mbps"),
    ("lbs", "lb"),
    ("months", "month"),
])
def test_alias_normalization(raw: str, canonical: str):
    assert normalize_unit(raw) == canonical


@pytest.mark.parametrize("value,src,dst,expected", [
    (1.0, "km", "m", 1000.0),
    (1.0, "nmi", "km", 1.852),
    (100.0, "kt", "km/h", 185.2),
    (60.0, "min", "hr", 1.0),
    (1.0, "hr", "min", 60.0),
    (2.0, "lb", "kg", 0.90718474),
    (1.0, "gbps", "mbps", 1000.0),
    (5280.0, "ft", "mi", 1.0),
])
def test_conversions(value, src, dst, expected):
    assert convert(value, src, dst) == pytest.approx(expected)


def test_roundtrip_is_identity():
    assert convert(convert(42.0, "km", "nmi"), "nmi", "km") == pytest.approx(42.0)


def test_unknown_unit_raises():
    with pytest.raises(UnsupportedConversion, match="unknown unit"):
        convert(1.0, "furlongs", "km")


def test_cross_family_raises():
    with pytest.raises(UnsupportedConversion, match="cannot convert"):
        convert(1.0, "kg", "km")
