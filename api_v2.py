"""Text-safe mesh API v2 encoder and bounded reference decoder (see API_V2.md)."""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import zlib

import radar_codec

PREFIX = "~W2"
MAX_BYTES = 140
MAX_PARTS = 16
MAX_RADAR_PARTS = 4
MAX_DECODED = 8192

# Index order is part of the wire protocol. Never reorder these fields.
SCHEMAS = {
    2: ("discovery", ("cmd", "lim", "u")),
    3: ("version", ("git_commit",)),
    4: ("report", ("status", "zip_code", "zip_codes")),
    5: ("pong", ("received_at", "path", "approx_direct_miles")),
    6: ("error", ("command", "error", "zip_code")),
    7: ("current", ("z", "l", "t", "c", "h", "i", "w", "a")),
}


def _uint(value: int) -> bytes:
    if not isinstance(value, int) or not 0 <= value < 2**64:
        raise ValueError("unsigned integer outside protocol range")
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def uint(self) -> int:
        value = 0
        for shift in range(0, 70, 7):
            if self.pos >= len(self.data):
                raise ValueError("truncated integer")
            byte = self.data[self.pos]
            self.pos += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                if value >= 2**64 or (shift and byte == 0):
                    raise ValueError("invalid integer")
                return value
        raise ValueError("oversized integer")


def _row(values: list) -> bytes:
    mask = sum(1 << i for i, value in enumerate(values) if value is not None)
    out = bytearray([mask])
    for value in values:
        if value is not None:
            if not isinstance(value, int):
                raise ValueError("weather fields must be integers or null")
            out.extend(_uint(value * 2 if value >= 0 else -value * 2 - 1))
    return bytes(out)


def _read_row(reader: _Reader) -> list:
    mask = reader.uint()
    if mask > 63:
        raise ValueError("invalid row presence mask")
    row = []
    for i in range(6):
        if mask & (1 << i):
            value = reader.uint()
            row.append(value // 2 if not value & 1 else -(value // 2) - 1)
        else:
            row.append(None)
    return row


def _weather(data: dict) -> bytes:
    zip_code = data["z"]
    if not re.fullmatch(r"[0-9]{5}", zip_code):
        raise ValueError("invalid ZIP")
    hourly, alerts = data["h"], data["a"]
    if len(hourly) > 5 or len(alerts) > 5 or len(data["n"]) != 6:
        raise ValueError("weather exceeds v2 schema")
    out = bytearray(_uint(int(zip_code)) + _uint(data["g"]))
    out.extend(_row(data["n"]))
    out.append(len(hourly) | (len(alerts) << 3) | (64 if data.get("x") else 0))
    previous = [0] * 6
    for row in hourly:
        if len(row) != 6:
            raise ValueError("invalid hourly row")
        out.extend(_row([None if v is None else v - (previous[i] or 0)
                         for i, v in enumerate(row)]))
        previous = row
    for code, severity in alerts:
        if not 0 <= code <= 9 or not 0 <= severity <= 4:
            raise ValueError("invalid alert code")
        out.append(code | (severity << 4))
    return bytes(out)


def _read_weather(payload: bytes) -> dict:
    reader = _Reader(payload)
    zip_code, generated = reader.uint(), reader.uint()
    if zip_code > 99999:
        raise ValueError("invalid ZIP")
    data = {"k": "w", "z": f"{zip_code:05d}", "g": generated,
            "n": _read_row(reader), "h": [], "a": []}
    counts = reader.uint()
    hours, alerts = counts & 7, (counts >> 3) & 7
    if counts > 127 or hours > 5 or alerts > 5:
        raise ValueError("invalid weather counts")
    previous = [0] * 6
    for _ in range(hours):
        delta = _read_row(reader)
        row = [None if v is None else v + (previous[i] or 0)
               for i, v in enumerate(delta)]
        data["h"].append(row)
        previous = row
    for _ in range(alerts):
        value = reader.uint()
        if value & 15 > 9 or value >> 4 > 4:
            raise ValueError("invalid alert code")
        data["a"].append([value & 15, value >> 4])
    if counts & 64:
        data["x"] = True
    if reader.pos != len(payload):
        raise ValueError("trailing weather bytes")
    return data


def encode(data: dict, *, flood_warning: bool = False,
           response_id: bytes | None = None) -> list[str]:
    """Encode a normalized response into <=16 independently armored messages."""
    if data.get("k") == "w":
        kind, payload = 1, _weather(data)
    elif data.get("k") == "r":
        kind, payload = 8, radar_codec.encode(data)
    else:
        kind = next((k for k, (name, _) in SCHEMAS.items()
                     if name == data.get("type")), None)
        if kind is None:
            raise ValueError("unknown response type")
        fields = SCHEMAS[kind][1]
        values = [data.get(field) for field in fields]
        while values and values[-1] is None:
            values.pop()
        payload = json.dumps(values, separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
    if len(payload) > MAX_DECODED:
        raise ValueError("response exceeds decoded size limit")
    compressor = zlib.compressobj(level=9, wbits=-15)
    compressed = compressor.compress(payload) + compressor.flush()
    flags = kind | (16 if flood_warning else 0)
    if len(compressed) < len(payload):
        payload, flags = compressed, flags | 32
    response_id = secrets.token_bytes(3) if response_id is None else response_id
    if len(response_id) != 3:
        raise ValueError("response ID must contain three bytes")
    # 102 raw bytes -> 136 Base64 characters + 3 marker bytes = 139.
    capacity = 98 if len(payload) <= 98 else 96
    total = max(1, (len(payload) + capacity - 1) // capacity)
    if total > MAX_PARTS or kind == 8 and total > MAX_RADAR_PARTS:
        raise ValueError("response exceeds fragment limit")
    messages = []
    for part in range(total):
        header = bytes([flags | (64 if total > 1 else 0)]) + response_id
        if total > 1:
            header += bytes([part, total])
        raw = header + payload[part * capacity:(part + 1) * capacity]
        messages.append(PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("="))
    return messages


def decode(messages: list[str]) -> dict:
    """Decode one complete response; caller must group by sender and expire buffers."""
    if not messages or len(messages) > MAX_PARTS * 2:
        raise ValueError("invalid message count")
    identity = None
    parts = {}
    for message in messages:
        if len(message.encode("utf-8")) > MAX_BYTES or not message.startswith(PREFIX):
            raise ValueError("invalid v2 frame")
        text = message[len(PREFIX):]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
            raise ValueError("invalid Base64url")
        try:
            raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid Base64url") from exc
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != text:
            raise ValueError("noncanonical Base64url")
        if len(raw) < 4 or raw[0] & 128:
            raise ValueError("invalid frame header")
        flags = raw[0]
        if flags & 64:
            if len(raw) < 7:
                raise ValueError("truncated fragment")
            part, total, payload = raw[4], raw[5], raw[6:]
            if not 2 <= total <= MAX_PARTS or part >= total:
                raise ValueError("invalid fragment index/count")
            if flags & 15 == 8 and total > MAX_RADAR_PARTS:
                raise ValueError("radar response exceeds fragment limit")
        else:
            part, total, payload = 0, 1, raw[4:]
        key = (flags, raw[1:4], total)
        if identity is not None and key != identity:
            raise ValueError("mixed response frames")
        identity = key
        if part in parts and parts[part] != payload:
            raise ValueError("conflicting duplicate fragment")
        parts[part] = payload
    if len(parts) != total:
        raise ValueError("incomplete response")
    payload = b"".join(parts[i] for i in range(total))
    if flags & 32:
        inflater = zlib.decompressobj(wbits=-15)
        try:
            payload = inflater.decompress(payload, MAX_DECODED + 1)
        except zlib.error as exc:
            raise ValueError("invalid compressed payload") from exc
        if len(payload) > MAX_DECODED or not inflater.eof or inflater.unused_data:
            raise ValueError("invalid or oversized compressed payload")
    kind = flags & 15
    if kind == 1:
        data = _read_weather(payload)
    elif kind == 8:
        data = radar_codec.decode(payload)
    elif kind in SCHEMAS:
        name, fields = SCHEMAS[kind]
        try:
            values = json.loads(payload)
        except (ValueError, RecursionError) as exc:
            raise ValueError("invalid positional payload") from exc
        if not isinstance(values, list) or len(values) > len(fields):
            raise ValueError("invalid positional schema")
        data = {"type": name, **dict(zip(fields, values))}
    else:
        raise ValueError("unknown message type")
    return {"id": identity[1].hex(), "flood_warning": bool(flags & 16), "data": data}
