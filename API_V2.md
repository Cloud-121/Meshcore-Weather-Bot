# WeatherBot Compact API v2

API v2 is an **explicitly opt-in machine protocol** for MeshCore text messages.
It minimizes reply bytes and message count while retaining the values in its
documented schemas. The receiving app must decode it before displaying it.

- Normal commands, channel alerts, and DM report notifications remain human-readable.
- Existing `json` commands and [JSON API v1](JSON_API.md) retain their formats.
- Only commands ending in `api2` receive `~W2` compact replies.
- [`api_v2.py`](api_v2.py) contains the reference encoder and bounded decoder.
- Compression uses Python's standard library; no new runtime dependency is needed.

## Quick start

Send `wx 60601 all api2` in a DM or on the configured Weather channel. Decode the
complete response in your app:

```python
from api_v2 import decode

# Group messages by bot identity, transport context, and response ID first.
result = decode(received_messages)
weather = result["data"]
print(weather["z"], weather["n"], weather["h"], weather["a"])
```

Send `bot api2` for discovery and cache the capabilities. Discovery is not needed
before every lookup. The simplest client allows one outstanding request per bot.
Response IDs group replies; they do not echo a client request ID.

## Commands

Commands are case-insensitive. ZIPs are five US digits; ZIP+4 is accepted and
normalized to the first five digits. Leading zeroes survive encoding.

| Request | Reply | Where |
| --- | --- | --- |
| `bot api2` | Capabilities | DM or Weather channel |
| `wx help api2` | Same capabilities | DM or Weather channel |
| `wx ZIPCODE all api2` | Binary current + up to five hours + coded alerts | DM or Weather channel |
| `wx ZIPCODE api2` | Current summary, with location and descriptions | DM or Weather channel |
| `wx version api2` | Git version | DM or Weather channel |
| `ping api2` | Receipt time, route, optional distance | DM or configured test channel |
| `wx report ZIPCODE api2` | Subscription acknowledgment | DM only |
| `wx report stop api2` | Unsubscribe acknowledgment | DM only |

Do not add `json`: `api2` selects the new format directly. Unsupported `api2`
commands in DM/Weather receive error 5. Disallowed channels are ignored, including
ping requests on Weather.

**Subscriptions:** acknowledgments are compact, but subsequent automatic
notifications remain human text. V2 uses the existing subscription state; it does
not introduce a binary notification subscription mode.

For the smallest forecast use `wx ZIPCODE all api2`. Its coded binary schema is
more efficient than the descriptive current-summary schema, despite containing
more forecast information. Requests are short ASCII commands fitting one message.

## Transport framing

```text
~W2<unpadded Base64url of header followed by payload bytes>
```

Use RFC 4648's URL-safe alphabet (`A-Z a-z 0-9 - _`), without `=` padding.
Restore padding to a multiple of four when decoding. The marker is case-sensitive.
Do not JSON-parse a wire message or concatenate complete armored messages.

### Header

| Byte | Meaning |
| --- | --- |
| 0 | Type in bits 0–3, flags in bits 4–6; bit 7 reserved, zero |
| 1–3 | Random 24-bit response ID, opaque bytes |
| 4 | Zero-based fragment index, **only if fragmented** |
| 5 | Total fragments, **only if fragmented** |
| Remaining | Payload or this fragment's payload slice |

| Flag mask | Meaning |
| --- | --- |
| `0x10` | Sender contact unavailable when preparing this DM reply |
| `0x20` | Complete payload uses raw DEFLATE |
| `0x40` | Fragment index/count present |

The contact flag replaces the long unknown-sender notice. It does not report the
final delivery route: a later retry can also fall back to flooding.

Single frames carry up to **98 payload bytes** plus a four-byte header. Fragmented
frames carry up to **96 payload bytes** plus a six-byte header. Both produce at
most **139 ASCII bytes**, within the configured 140-byte budget.

### Reassembly

The encoder creates the complete payload, optionally compresses it, then splits
it. All fragments share flags, type, ID, and total count. Fragment count is 2–16;
single frames omit fragment metadata entirely.

1. Group by bot identity, transport/channel context, and response ID.
2. Require consistent flags and total count.
3. Accept reordered fragments and identical duplicates; reject conflicting copies.
4. Wait for every index from zero through `total - 1`.
5. Concatenate **decoded payload slices** in index order.
6. Decompress once if flagged, then decode the selected schema.

Expire incomplete responses after a bounded interval (60 seconds is a reasonable
client default), and limit pending groups. `decode()` accepts a complete group,
including up to 32 input frames to accommodate duplicates. It does not implement
a network buffer or timeout. Incomplete groups raise `ValueError`.

After a timeout clients may retry with a fresh sender timestamp. There is no
selective fragment retransmission command. IDs are not globally unique: do not
group different senders or retain completed IDs indefinitely. MeshCore supplies
transport encryption, integrity, and DM acknowledgments; v2 adds no checksum.

### Compression and limits

Raw DEFLATE is RFC 1951, **without** zlib/gzip headers or trailers. In Python use
`zlib.decompressobj(wbits=-15)`. It is selected only when strictly smaller than
the original payload, including for binary weather. No shared dictionary or
previous response is needed.

Limit decompressed output to **8192 bytes**, require stream completion, and reject
trailing streams. The encoder rejects responses exceeding that limit or requiring
more than 16 fragments. The bot returns error 3 if response encoding fails rather
than silently dropping data.

## Type 1: binary weather

Decoded object:

```json
{
  "k":"w", "z":"00601", "g":1780000000,
  "n":[68,2,50,225,10,77],
  "h":[[0,68,2,225,10,10],[60,69,2,225,10,10]],
  "a":[[6,2]]
}
```

| Field | Meaning |
| --- | --- |
| `z` | Five-digit ZIP string |
| `g` | Generation time, Unix seconds UTC |
| `n` | `[temperature_F, weather_code, humidity_percent, wind_degrees, wind_mph, apparent_temperature_F]` |
| `h` | Rows `[minutes_after_g, temperature_F, weather_code, wind_degrees, wind_mph, precipitation_percent]` |
| `a` | Rows `[alert_code, severity_code]`; empty means no coded alerts in this response |
| `x` | Present and true when source rows exceeded the five-hour/five-alert caps |

The current provider uses Open-Meteo apparent temperature for the last current
column; it is not necessarily a heat index. The current combined-weather provider
returns an empty alert list; automatic NOAA notifications use a separate path.
Compression preserves normalized integers without additional rounding or wind
quantization. The five-hour/five-alert caps are inherited from the curated API.

### Integer primitives

Unsigned integers use canonical unsigned LEB128: low seven bits per byte,
least-significant group first, high bit set when another byte follows. Values are
below `2^64`. Reject redundant leading groups and oversized integers. JavaScript
ports should use `BigInt` or explicit safe-range checks for this general primitive.

Signed values use ZigZag followed by unsigned LEB128:

```text
encode(n) = 2*n       if n >= 0, otherwise -2*n - 1
decode(u) = u/2       if even,   otherwise -(u+1)/2
```

### Payload layout

1. ZIP as unsigned LEB128; reconstruct with five-digit zero padding.
2. Generation time as unsigned LEB128.
3. Current row, encoded as below.
4. One count byte: bits 0–2 hourly count, bits 3–5 alert count, bit 6 truncation,
   bit 7 zero. Counts are each 0–5.
5. Hourly delta rows.
6. One byte per alert: low nibble alert code, high nibble severity.

**Rows:** one presence byte, bits 0–5 identifying the six columns, followed by a
signed ZigZag integer for each present column in order. Bits 6–7 are zero. Missing
columns decode to `null`; zero is a present value.

**Hourly deltas:** start with six zeroes as the previous row. For each non-null
column transmit `current - previous`, treating null previous values as zero.
After each row replace the previous row with the reconstructed row. Null current
columns remain null. This preserves irregular time intervals and null transitions.
Current conditions are not the delta baseline. Reject trailing payload bytes.

### Codes

Weather: `0` unknown/other, `1` clear, `2` partly cloudy, `3` mostly cloudy,
`4` cloudy, `5` rain, `6` thunderstorm, `7` snow/ice, `8` fog/haze, `9` wind.

Alerts: `0` other, `1` tornado, `2` thunder/lightning, `3` flood, `4` wind,
`5` winter, `6` heat, `7` hurricane/tropical, `8` fire, `9` air quality.

Severity: `0` unknown, `1` minor, `2` moderate, `3` severe, `4` extreme.

## Types 2–7: positional payloads

These are compact UTF-8 JSON **arrays**, optionally DEFLATE-compressed. Field names
and object wrappers are omitted; dynamic strings such as location and route are
retained. Trailing null slots are omitted, interior missing slots are `null`.
Clients must accept absent/null optional slots. The reference decoder reconstructs
named objects according to this table:

| Type | Decoded `type` | Array slots in order |
| --- | --- | --- |
| 2 | `discovery` | `cmd`, `lim`, `u` |
| 3 | `version` | `git_commit` |
| 4 | `report` | `status`, `zip_code`, `zip_codes` |
| 5 | `pong` | `received_at`, `path`, `approx_direct_miles` |
| 6 | `error` | `command`, `error`, `zip_code` |
| 7 | `current` | `z`, `l`, `t`, `c`, `h`, `i`, `w`, `a` |

Reject unknown types, reserved flags, and extra array slots. Schema index order is
part of the protocol and must not be reordered by implementations.

### Discovery

```json
[127,[140,16],["F","mph","%"]]
```

`cmd` capability bits: 0 discovery/help, 1 current summary, 2 combined weather,
3 version, 4 ping, 5 report enable, 6 report stop. `lim` is maximum message bytes
and maximum fragments. `u` supplies standard units. Help sends capabilities instead
of static prose; the client provides instructions and display labels.

### Subscriptions

Enable: `["enabled","00601"]`.

Stop: `["stopped",null,["00601","60601"]]`. An empty list means nothing was
subscribed. Fixed stop-command instructions are supplied by the client.

### Ping, version, and current summary

Ping carries an ISO-8601 UTC receipt time, the existing route description, and
optional distance in miles to one decimal place. This is endpoint distance, not
relay-path distance. Version carries the Git commit identifier or existing fallback.

Current fields match `wx ZIPCODE json`: ZIP, location, temperature, description,
humidity, apparent temperature, wind description, optional alert rows. Interior
nulls can appear where legacy JSON omitted a key.

### Errors

`command` is `wx`, `wx report`, or `api2`; `error` is numeric; ZIP is optional.
The client supplies human-readable/localized explanations.

| Code | Meaning |
| --- | --- |
| 0 | Other safe lookup failure |
| 1 | ZIP not found |
| 2 | Weather service/current data unavailable |
| 3 | Invalid provider data or response exceeds encoding limits |
| 4 | Subscription operation requires DM |
| 5 | Unsupported/malformed API v2 command |

## Interoperability vector

Uncompressed error with ID `010203`, no flags:

```text
Decoded:      {"type":"error","command":"wx","error":1}
Header hex:   06 01 02 03
Payload UTF8: ["wx",1]
Wire:         ~W2BgECA1sid3giLDFd
```

```python
from api_v2 import decode, encode

assert encode(
    {"type": "error", "command": "wx", "error": 1},
    response_id=bytes.fromhex("010203"),
) == ["~W2BgECA1sid3giLDFd"]
assert decode(["~W2BgECA1sid3giLDFd"])["data"]["error"] == 1
```

`decode()` returns `{"id": "<six hex digits>", "flood_warning": false,
"data": <named object>}`. Invalid inputs raise `ValueError`.

### Weather vectors

Uncompressed weather with ID `010203`, ZIP `00601`, generation `1780000000`,
six null current fields, and empty hourly/alert arrays:

```text
~W2AQECA9kEgMri0AYAAA
```

Raw-DEFLATE weather with the same ID, ZIP, and generation:

```text
~W2IQECA7vJ0nDq0QU2-w5GlpRDzCKzGHntGYBsIFPEvoKJAQiwUmoA
```

The compressed vector decodes to:

```json
{
  "k":"w", "z":"00601", "g":1780000000,
  "n":[68,2,50,225,10,77],
  "h":[[0,68,2,225,10,10],[60,69,2,225,10,10],
       [120,70,2,225,10,10],[180,71,2,225,10,10],
       [240,72,2,225,10,10]],
  "a":[[6,2]]
}
```

Compliant compressors may produce different DEFLATE bytes. Decoders must recover
the same data; exact compressed-byte equality is not required.

## Measured savings and verification

Run `python benchmark_api_v2.py` for deterministic five-hour fixtures:

| Fixture | v1 bytes / messages | v2 bytes / messages |
| --- | --- | --- |
| Normal | 299 / 3 | 49 / 1 |
| Five alerts | 341 / 3 | 53 / 1 |
| Freezing | 317 / 3 | 49 / 1 |
| Missing values | 311 / 3 | 46 / 1 |

Framing and Base64 overhead are included: these fixtures save **67% of reply
messages**. Actual values affect size; larger descriptive replies can fragment.
These are application-message measurements, excluding RF headers, relays,
acknowledgments, and retries. Live companion/radio validation is needed to measure
those effects.

With Python 3.10+ and project requirements installed:

```sh
python -m unittest -v
python benchmark_api_v2.py
```

`test_api_v2.py` covers round trips, missing/extreme values, packet budgets,
fragmentation, duplicates, malformed inputs, and opt-in compatibility.
