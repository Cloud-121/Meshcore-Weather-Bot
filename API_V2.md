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
| `wx radar LAT LON TIME api2` | 50-mile circular observed/forecast reflectivity grid | DM or Weather channel |
| `wx ZIPCODE api2` | Current summary, with location and descriptions | DM or Weather channel |
| `wx version api2` | Git version | DM or Weather channel |
| `ping api2` | Receipt time, route, optional distance | DM or configured test channel |
| `wx report ZIPCODE api2` | Subscription acknowledgment | DM only |
| `wx report stop api2` | Unsubscribe acknowledgment | DM only |

Do not add `json`: `api2` selects the new format directly. Unsupported `api2`
commands in DM/Weather receive error 5. Disallowed channels are ignored, including
ping requests on Weather.

Radar `TIME` is `now` or an explicitly signed whole-hour offset from `-5h` through
`+5h`. `now` and non-positive offsets return observed MRMS reflectivity; positive
offsets return HRRR simulated reflectivity and must be presented as a forecast.
Each request returns one snapshot. Initial provider coverage is CONUS only.

### Radar request flow

The phone sends the complete command as one ordinary MeshCore text message. Latitude
comes before longitude and both use signed decimal degrees:

```text
wx radar 30.4515 -91.1871 now api2
wx radar 30.4515 -91.1871 -5h api2
wx radar 30.4515 -91.1871 +5h api2
```

The request does not contain a client request ID. The phone should normally allow
only one outstanding radar request per bot. The bot performs these steps:

1. Record its UTC receipt time and validate the coordinates and hour offset.
2. For `now` or a negative offset, select the newest MRMS observation at or before
   the requested time. It must be within 15 minutes of that time.
3. For a positive offset, select an available HRRR run and the forecast hour nearest
   the requested valid time. HRRR valid times are hourly.
4. Build a north-up 32-by-32 conceptual grid spanning 100 miles across each axis.
5. Keep the 812 cell centers inside the 50-mile-radius circle and sample the nearest
   NOAA reflectivity-grid point for each center.
6. Quantize each reflectivity value to one of the eight three-bit codes below.
7. Build the 19-byte metadata header followed by 305 packed cell bytes.
8. Apply raw DEFLATE only if it makes the complete 324-byte payload smaller.
9. Split the resulting bytes into at most four API v2 fragments and Base64url-armour
   each fragment for MeshCore's text transport.

The reply is therefore not a PNG, map tile, JSON document, or sequence of ASCII
zeroes and ones. It is binary metadata and bit-packed grid values carried inside
Base64url text. The Flutter app reassembles those bytes and draws the cells over its
own map.

The bot's 24-bit response ID is generated for the reply and is not related to the
coordinates or requested hour. It exists only to group that reply's fragments.

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

Type 8 radar responses are additionally limited to four fragments. This is a hard
application-response limit; radio acknowledgments, retries, and flood fallback are
transport activity and can result in additional RF transmissions.

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

## Type 8: circular radar grid

Radar uses a fixed binary schema. Multi-byte integers are big-endian. Version 1 has
a fixed 50-statute-mile (80,467.2 metre) radius.

### Payload size

The normal width is 32. Its uncompressed payload has a fixed worst-case size:

```text
metadata                 19 bytes
812 cells x 3 bits     2,436 bits
packed cells       ceil(2436/8) = 305 bytes
complete payload         324 bytes
```

Four fragmented messages can carry 384 payload bytes, so the 324-byte form always
fits even when the grid is high entropy and cannot be compressed. Clear or uniform
weather often compresses into fewer messages. Clients must accept any count from one
through four and must not infer grid dimensions from fragment count.

Width 28 is reserved as a lower-resolution profile. It contains 616 circular cells,
requiring 231 packed bytes and 250 total uncompressed payload bytes. The width byte
always tells the client which profile was returned.

### Metadata header

| Bytes | Meaning |
| --- | --- |
| 0 | Source: `0` observed MRMS, `1` forecast HRRR; bits 1–7 zero |
| 1 | Square grid width: currently `28` or `32` |
| 2 | Requested signed hour offset as an 8-bit two's-complement integer |
| 3–6 | Center latitude in signed degrees times 100,000 |
| 7–10 | Center longitude in signed degrees times 100,000 |
| 11–14 | Product valid time, unsigned Unix seconds UTC |
| 15–18 | Forecast issue time, unsigned Unix seconds UTC; zero for observations |
| 19–end | Three-bit reflectivity codes, packed least-significant bit first |

For the decoded object, these bytes map to fields as follows:

| Field | Meaning |
| --- | --- |
| `k` | Always `r`; identifies radar after decoding |
| `lat`, `lon` | Actual encoded center in decimal degrees |
| `o` | Requested whole-hour offset, `-5` through `5` |
| `v` | Actual product valid time as Unix seconds UTC |
| `i` | HRRR model issue time; present for forecasts only |
| `s` | `observed` or `forecast` |
| `n` | Conceptual square width, currently 28 or 32 |
| `c` | Circular cell codes in wire order |

Do not calculate the displayed timestamp as receipt time plus `o`. Use `v`: MRMS
has publication latency and HRRR is restricted to hourly valid times. For forecasts,
`i` allows the UI to show which model run generated the result.

### Cell order and map placement

The conceptual square is north-up. Cells are visited row-major from north to south
and west to east, but a cell is transmitted only when its center is inside the
circle. For zero-based `(x,y)` and width `n`, include it when:

```text
(2*x + 1 - n)^2 + (2*y + 1 - n)^2 <= n^2
```

This produces 616 values for width 28 and 812 for width 32. For each included cell,
let `east=((x+0.5)/n*2-1)*80.4672` km and
`north=(1-(y+0.5)/n*2)*80.4672` km. Its center is the great-circle destination from
the encoded center with distance `hypot(east,north)` on a 6371.0088 km sphere and
initial bearing `atan2(east,north)`. Normalize longitude to `[-180,180)`.

Within a byte, the first cell starts at bit zero; values crossing a byte boundary
continue in the next byte. Unused high bits in the final byte are zero. The receiver
computes geographic cell centers over the 100-mile diameter from the encoded center;
no per-cell coordinates are transmitted.

The app can either render each included cell as a small polygon centered at that
computed position or first reconstruct a square `n*n` array. When reconstructing a
square, iterate every `(x,y)` in row-major order, apply the circle test, and consume
one `c` value only for included cells. Values outside the circle are not present in
the payload and should remain transparent.

This distinction is important:

- Outside circle: no value was transmitted; render transparent.
- Code 0: NOAA returned a valid dry/below-threshold value.
- Code 7: NOAA data was missing or had no coverage; render transparent or with a
  separate unavailable-data treatment, but never as clear weather.

### Three-bit packing

Cell codes are appended to one continuous little-endian bit stream. For cell index
`j`, its three bits begin at bit offset `j*3`. This is independent of byte
boundaries. Decoder pseudocode is:

```text
bit = 0
for every expected cell:
    byteIndex = bit ~/ 8
    shift = bit % 8
    value = packed[byteIndex] >> shift
    if shift > 5:
        value |= packed[byteIndex + 1] << (8 - shift)
    cells.add(value & 7)
    bit += 3
```

For example, codes `[1,2,7]` become bytes `D1 01`. The third code crosses the byte
boundary. Any unused high bits in the final packed byte must be zero.

| Code | Composite reflectivity |
| --- | --- |
| 0 | Dry/below 5 dBZ |
| 1 | 5 to below 20 dBZ |
| 2 | 20 to below 30 dBZ |
| 3 | 30 to below 40 dBZ |
| 4 | 40 to below 50 dBZ |
| 5 | 50 to below 60 dBZ |
| 6 | 60 dBZ or greater |
| 7 | Missing/no coverage; never render this as dry |

Decoded reference object:

```json
{"k":"r","lat":30.4515,"lon":-91.1871,"o":3,"v":1780000000,
 "i":1779992800,"s":"forecast","n":32,"c":[0,0,1,2]}
```

The sample `c` above is abbreviated. Observations omit `i`. Clients must label
`s=forecast` as model guidance rather than observed radar. Always display or retain
the actual valid time because source cadence and publication delay mean it can
differ from the requested wall-clock time.

### Complete transmission sequence

For an uncompressible width-32 response, the four bot messages have this logical
form. Values in angle brackets are binary before Base64url encoding:

```text
~W2<Base64url: type/flags + response ID + index 0 + count 4 + payload bytes 0..95>
~W2<Base64url: type/flags + response ID + index 1 + count 4 + payload bytes 96..191>
~W2<Base64url: type/flags + response ID + index 2 + count 4 + payload bytes 192..287>
~W2<Base64url: type/flags + response ID + index 3 + count 4 + payload bytes 288..323>
```

For type 8 without compression or a contact warning, transport byte 0 is `0x48`:
type 8 (`0x08`) plus fragmented (`0x40`). With raw DEFLATE it is `0x68`; with the
DM contact warning it is `0x58` uncompressed or `0x78` compressed. These transport
flags are separate from byte 0 of the reassembled radar payload.

Each full fragment consists of a six-byte transport header and up to 96 payload
bytes. Base64url expands a full 102-byte binary frame to 136 characters; adding the
three-character `~W2` marker produces 139 ASCII bytes. The uncompressible 324-byte
benchmark produces wire-message lengths `139, 139, 139, 59`, totaling 476 ASCII
bytes across four MeshCore application messages.

### Flutter decoding outline

The Flutter client should perform transport reassembly before decoding radar:

```dart
// Outline only: keep bounded buffers and validate every field as described above.
final encoded = message.substring(3); // Remove the case-sensitive "~W2" marker.
final raw = base64Url.decode(base64Url.normalize(encoded));
final transport = raw[0];
final kind = transport & 0x0f;
final compressed = (transport & 0x20) != 0;
final fragmented = (transport & 0x40) != 0;
final responseId = raw.sublist(1, 4);
final index = fragmented ? raw[4] : 0;
final count = fragmented ? raw[5] : 1;
final slice = raw.sublist(fragmented ? 6 : 4);
```

Group slices by bot identity, MeshCore context, and the three response-ID bytes.
After all indexes are present, concatenate slices in index order. If `compressed`
is set, decode the concatenated payload once with raw DEFLATE, for example
`ZLibDecoder(raw: true)`. Do not decompress each fragment separately.

Then require `kind == 8`, read the 19-byte radar header using `ByteData` with
`Endian.big`, calculate the expected circular cell count, verify the exact packed
length, and unpack the three-bit values. In particular:

```dart
final view = ByteData.sublistView(payload);
final sourceFlags = view.getUint8(0);
final width = view.getUint8(1);
final requestedOffset = view.getInt8(2);
final latitude = view.getInt32(3, Endian.big) / 100000.0;
final longitude = view.getInt32(7, Endian.big) / 100000.0;
final validUnix = view.getUint32(11, Endian.big);
final issuedUnix = view.getUint32(15, Endian.big);
final packedCells = payload.sublist(19);
```

Reject reserved source flag bits, unsupported widths, offsets outside `-5..5`, bad
coordinates, inconsistent observation/forecast issue times, an unexpected cell
count, nonzero padding bits, or trailing bytes. Expire an incomplete response rather
than drawing a partial radar grid.

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
[255,[140,16],["F","mph","%"]]
```

`cmd` capability bits: 0 discovery/help, 1 current summary, 2 combined weather,
3 version, 4 ping, 5 report enable, 6 report stop, 7 radar. `lim` is maximum message bytes
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

`command` is `wx`, `wx report`, `radar`, or `api2`; `error` is numeric; ZIP is optional.
The client supplies human-readable/localized explanations.

| Code | Meaning |
| --- | --- |
| 0 | Other safe lookup failure |
| 1 | ZIP not found |
| 2 | Weather service/current data unavailable |
| 3 | Invalid provider data or response exceeds encoding limits |
| 4 | Subscription operation requires DM |
| 5 | Unsupported/malformed API v2 command |
| 6 | Radar location outside supported coverage |
| 7 | Requested radar observation/forecast unavailable |

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
| 32-wide high-entropy radar | n/a | 476 / 4 |

Framing and Base64 overhead are included: these fixtures save **67% of reply
messages**. Actual values affect size; larger descriptive replies can fragment.
The radar fixture uses uniformly randomized cell codes so it verifies the hard
four-message budget without relying on favorable weather compression. These are
application-message measurements, excluding RF headers, relays,
acknowledgments, and retries. Live companion/radio validation is needed to measure
those effects.

With Python 3.10+ and project requirements installed:

```sh
python -m unittest -v
python benchmark_api_v2.py
```

`test_api_v2.py` covers round trips, missing/extreme values, packet budgets,
fragmentation, duplicates, malformed inputs, and opt-in compatibility.
