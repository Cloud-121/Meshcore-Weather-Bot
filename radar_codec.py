"""Compact binary codec for 50-mile circular radar grids."""

from __future__ import annotations

import struct

RADIUS_MILES = 50
SUPPORTED_WIDTHS = (28, 32)
_HEADER = struct.Struct(">BBbiiII")


def circle_cell_count(width: int) -> int:
    if width not in SUPPORTED_WIDTHS:
        raise ValueError("unsupported radar grid width")
    return sum(
        (2 * x + 1 - width) ** 2 + (2 * y + 1 - width) ** 2 <= width**2
        for y in range(width)
        for x in range(width)
    )


def circle_cells(width: int):
    """Yield included cells in north-to-south, west-to-east wire order."""
    if width not in SUPPORTED_WIDTHS:
        raise ValueError("unsupported radar grid width")
    for y in range(width):
        for x in range(width):
            if (2 * x + 1 - width) ** 2 + (2 * y + 1 - width) ** 2 <= width**2:
                yield x, y


def _pack_cells(cells: list[int]) -> bytes:
    output = bytearray((len(cells) * 3 + 7) // 8)
    bit = 0
    for value in cells:
        if not isinstance(value, int) or not 0 <= value <= 7:
            raise ValueError("radar cells must contain codes 0 through 7")
        byte, shift = divmod(bit, 8)
        output[byte] |= value << shift & 0xff
        if shift > 5:
            output[byte + 1] |= value >> (8 - shift)
        bit += 3
    return bytes(output)


def _unpack_cells(payload: bytes, count: int) -> list[int]:
    cells = []
    bit = 0
    for _ in range(count):
        byte, shift = divmod(bit, 8)
        value = payload[byte] >> shift
        if shift > 5:
            value |= payload[byte + 1] << (8 - shift)
        cells.append(value & 7)
        bit += 3
    if count * 3 % 8 and payload[-1] >> (count * 3 % 8):
        raise ValueError("nonzero radar padding bits")
    return cells


def encode(data: dict) -> bytes:
    source = data["s"]
    if source not in ("observed", "forecast"):
        raise ValueError("invalid radar source")
    width = data["n"]
    cells = data["c"]
    if len(cells) != circle_cell_count(width):
        raise ValueError("incorrect radar cell count")
    offset = data["o"]
    if not isinstance(offset, int) or not -5 <= offset <= 5:
        raise ValueError("invalid radar time offset")
    latitude = round(float(data["lat"]) * 100000)
    longitude = round(float(data["lon"]) * 100000)
    if not -9000000 <= latitude <= 9000000 or not -18000000 <= longitude <= 18000000:
        raise ValueError("invalid radar coordinates")
    valid = data["v"]
    issued = data.get("i", 0)
    if not all(isinstance(value, int) and 0 <= value <= 0xffffffff
               for value in (valid, issued)):
        raise ValueError("invalid radar timestamp")
    if source == "observed" and issued:
        raise ValueError("observations cannot have an issue time")
    if source == "forecast" and not issued:
        raise ValueError("forecasts require an issue time")
    return _HEADER.pack(source == "forecast", width, offset, latitude, longitude,
                        valid, issued) + _pack_cells(cells)


def decode(payload: bytes) -> dict:
    if len(payload) < _HEADER.size:
        raise ValueError("truncated radar payload")
    flags, width, offset, latitude, longitude, valid, issued = _HEADER.unpack_from(payload)
    if flags & ~1:
        raise ValueError("invalid radar flags")
    count = circle_cell_count(width)
    packed_size = (count * 3 + 7) // 8
    if len(payload) != _HEADER.size + packed_size:
        raise ValueError("invalid radar payload size")
    if not -5 <= offset <= 5 or not -9000000 <= latitude <= 9000000 \
            or not -18000000 <= longitude <= 18000000:
        raise ValueError("invalid radar metadata")
    if not flags and issued:
        raise ValueError("observation has an issue time")
    if flags and not issued:
        raise ValueError("forecast has no issue time")
    result = {
        "k": "r", "lat": latitude / 100000, "lon": longitude / 100000,
        "o": offset, "v": valid, "s": "forecast" if flags else "observed",
        "n": width, "c": _unpack_cells(payload[_HEADER.size:], count),
    }
    if issued:
        result["i"] = issued
    return result
