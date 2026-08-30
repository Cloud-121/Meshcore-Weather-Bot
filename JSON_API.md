# Mesh JSON API v1

This is the app-facing protocol for the pyMC/openHop Weather Bot. It is designed for
MeshCore's 140-byte message limit. The older `json` commands are unchanged; clients
that need this protocol must append `api` exactly as shown below (command matching is
case-insensitive).

## Commands

| Request | Purpose |
| --- | --- |
| `bot json api` | Discover this bot's API version and capabilities. |
| `wx ZIPCODE json all api` | Get current conditions, up to five hourly forecasts, and active alerts. |

Requests work in a DM or the configured Weather channel. The ZIP must be a US
five-digit ZIP (ZIP+4 is accepted and reduced to five digits).

## Fragment envelope

Every reply message is a complete JSON object:

```json
{"v":1,"i":"a1b2c3","p":1,"n":3,"d":{"k":"wx"}}
```

- `v`: API protocol version (currently `1`)
- `i`: response ID; group messages with the same ID
- `p`: one-based part number
- `n`: total number of parts, always 1–3
- `d`: one partial response object
- `w`: optional `1`, meaning the DM was flood-sent because the bot lacks the sender's advert

Collect all `n` envelopes with the same `i` and order them by `p`. Copy ordinary `d`
keys into one response object; when `h` appears in multiple parts, concatenate its
arrays in part order. Each part is independently valid JSON, so do not concatenate
complete mesh messages.

## Discovery response

`bot json api` returns one envelope whose `d` contains:

```json
{"k":"b","cmd":3,"lim":[140,3],"u":["F","mph","%"]}
```

`k` is `"b"` (bot discovery), `cmd` is capability bitmask `3` (bit 1: discovery;
bit 2: all-weather), `lim` is `[maximum_message_bytes, maximum_message_count]`, and
`u` gives temperature, wind, and humidity/precipitation units. Discovery deliberately
uses no display text; this document defines all codes.

## Weather response

After merging, a normal `wx ZIPCODE json all api` response has this shape:

```json
{
  "k":"w",
  "z":"60601",
  "g":1780000000,
  "n":[68,2,50,225,10,77],
  "h":[[0,68,2,225,10,10],[60,69,1,225,9,0]],
  "a":[[6,2]],
  "x":true
}
```

- `z`: ZIP
- `g`: generation time as Unix seconds (UTC)
- `n`: current row in `[temperature_F, weather_code, humidity_percent, wind_direction_degrees, wind_mph, heat_index_F]` order. Heat index is `null` when NWS does not report one.
- `h`: hourly rows in `[minutes_after_g, temperature_F, weather_code, wind_direction_degrees, wind_mph, precipitation_percent]` order. The bot returns no more than the next five rows. Optional source values are `null`.
- `a`: alert rows in `[alert_code, severity_code]` order; an empty array means no active alerts
- `x`: present and `true` only when more than five alerts were available

Weather codes: `0` unknown/other, `1` clear, `2` partly cloudy, `3` mostly cloudy,
`4` cloudy, `5` rain, `6` thunderstorm, `7` snow/ice, `8` fog/haze, `9` wind.
Alert codes: `0` other, `1` tornado, `2` thunder/lightning, `3` flood, `4` wind,
`5` winter, `6` heat, `7` hurricane/tropical, `8` fire, `9` air quality. Severity
codes: `0` unknown, `1` minor, `2` moderate, `3` severe, `4` extreme.

The API is a compact curated forecast, not a raw NWS response. It contains no
display-oriented text. Values are normalized to Fahrenheit, mph, percent, and numeric
codes. To guarantee at most three 140-byte messages, it retains no more than five
active alerts and always prefers retaining current data and five hourly rows.

## Errors

An API failure is also framed. Merge its `d` object and inspect:

```json
{"k":"e","c":1,"z":"60601","e":1}
```

`c` is command code `1` (`wx`), `z` is supplied when available, and `e` is error
code `1` ZIP not found, `2` service/current data unavailable, `3` invalid provider
data, or `0` another safe failure. Apps should treat unknown keys as forward-compatible
additions.
