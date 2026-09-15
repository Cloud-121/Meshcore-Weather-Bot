# WeatherBot Response Review

Examples use placeholder values in angle brackets. Lines shown together are sent as one response, though long messages can be split to fit the mesh limit.

## `wx ZIPCODE`

```text
☀️ <City>, <ST> <ZIPCODE>
🌡️ <temperature>°F · <conditions>
☀️ Feels like <apparent temperature>°F · 💧 <humidity>% · 💨 <direction> <speed> mph
```

The feels-like portion is present when Open-Meteo supplies an apparent temperature.
Normal weather replies do not include alerts; NWS alerts are sent only through automatic
alert monitoring and `wx report` subscriptions.

### Lookup error

```text
WX <ZIPCODE>: lookup failed: <reason>.
```

Possible reasons include `ZIP code was not found`, `weather service is unavailable`, and `current conditions are unavailable`.

## `wx help`

```text
Gulf Coast Mesh bot, Designed by ScarlettOSA
wx ZIPCODE: weather report
wx report ZIPCODE: DM alert signup
wx report stop: stop DM alerts
wx version: running Git commit
ping: DM or #test
```

## `wx version`

```text
Gulf Coast Mesh Bot version: <Git commit>
```

## `wx report ZIPCODE` (DM only)

```text
WX reports enabled for <ZIPCODE>. To stop these alerts: wx report stop
```

## `wx report stop` (DM only)

With subscriptions removed:

```text
WX reports stopped.
```

With no active subscriptions:

```text
You do not have any active WX reports.
```

Sent in a channel instead of a DM:

```text
Please run wx report ZIPCODE or wx report stop in a DM.
```

## `ping`

```text
@[<user>] 🏓 Pong
Received: <UTC time, HH:MM:SS.mmm>
Path: <raw route hashes, hop count, or unavailable>
Region: <resolved MeshCore region, when supplied>
Hops: <hop count, when known>
Message Type: <Flood, Direct, TC Flood, or TC Direct when RF-log verified>
Approx. direct distance: <miles> mi
```

The distance line is present only when the bot and a DM sender have advertised GPS
coordinates. It is straight-line endpoint distance, not the distance through relays.
For `#test`, raw hashes are uppercase and dash-separated (for example, `AF-2B-8A` or
`AF2B-8A10`); unmatched packets and DMs fall back to hop count.
The region line is included only when the companion supplies an already-resolved region
name; this bot does not attempt to reverse-map MeshCore transport codes.
The message-type line is shown only for a channel packet that exactly matches an RF-log
entry; DMs and unmatched channel packets omit it.

## NWS alert notification

Channel alert:

```text
🚨 NWS ALERT: <ZIPCODE>[,<ZIPCODE>]
⚠️ <event> (<severity>, <urgency>, until <end time>)
<NWS description>
```

If the NWS alert includes instructions not already in the description, they are appended to the final line. Personal alert subscriptions use the same format and add:

```text
To stop these alerts: wx report stop
```

## Delivery notice

This is appended to any text reply sent by flood because the sender has not advertised a route:

```text
⚠️ This reply was flood-sent because I do not have your advert. Please send an advert for reliable future replies.
```

## JSON replies

Appending `json` to a command returns compact JSON rather than the text above. Representative shapes are below.

### `wx ZIPCODE json`

The weather JSON uses short keys to keep mesh messages small:

```json
{
  "z": "<ZIPCODE>",
  "l": "<City>, <ST>",
  "t": 72,
  "c": "<conditions>",
  "h": 50,
  "i": 77,
  "w": "<direction> <speed> mph"
}
```

`z` is ZIP code, `l` is location, `t` is temperature in °F, `c` is conditions,
`h` is humidity percent, `i` is apparent temperature in °F, and `w` is wind. Missing
weather fields are omitted.

### `wx help json`

```json
{
  "type": "help",
  "service": "Gulf Coast Mesh Bot",
  "attribution": "Designed by ScarlettOSA",
  "commands": ["wx ZIPCODE", "wx report ZIPCODE", "wx report stop", "wx version", "ping"],
  "json_modifier": "Append json to a command for a structured response."
}
```

### Other JSON replies

```json
{"type":"version","git_commit":"<Git commit>"}
{"type":"report","status":"enabled","zip_code":"<ZIPCODE>","stop_command":"wx report stop"}
{"type":"report","status":"stopped","zip_codes":["<ZIPCODE>"]}
{"type":"pong","received_at":"<UTC ISO-8601 time>","path":"<route>","approx_direct_miles":12.3}
{"type":"error","command":"wx","zip_code":"<ZIPCODE>","error":"<reason>"}
{"type":"error","command":"wx report","error":"run this command in a DM"}
```

For an unknown sender, JSON responses gain this field:

```json
{"delivery_warning":"⚠️ This reply was flood-sent because I do not have your advert. Please send an advert for reliable future replies."}
```
