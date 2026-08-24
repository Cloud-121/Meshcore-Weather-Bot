# WeatherBot Response Review

Examples use placeholder values in angle brackets. Lines shown together are sent as one response, though long messages can be split to fit the mesh limit.

## `wx ZIPCODE`

```text
☀️ <City>, <ST> <ZIPCODE>
🌡️ <temperature>°F · <conditions>
💧 <humidity>% · 💨 <direction> <speed> mph
✅ No active NWS alerts
```

When the latest observation is unavailable, the weather section can instead be:

```text
☀️ <City>, <ST> <ZIPCODE>
🌡️ <temperature>°F · <forecast>
💨 <direction> <speed>
(current-hour NWS forecast)
✅ No active NWS alerts
```

When there is an active alert, the final line is one line per alert instead:

```text
⚠️ <alert event> (<severity>, until <end time>)
```

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
Add json for structured output
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
🏓 Pong
Received: <UTC ISO-8601 time>
Path: <route, hop count, or unavailable>
```

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
  "w": "<direction> <speed> mph",
  "a": [["<alert event>", "<severity>", "<end time>"]]
}
```

`z` is ZIP code, `l` is location, `t` is temperature in °F, `c` is conditions, `h` is humidity percent, `w` is wind, and `a` is alerts. Each alert contains event, severity, and—when available—its end time. Missing weather fields are omitted; no active alerts are `"a":[]`.

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
{"type":"pong","received_at":"<UTC ISO-8601 time>","path":"<route>"}
{"type":"error","command":"wx","zip_code":"<ZIPCODE>","error":"<reason>"}
{"type":"error","command":"wx report","error":"run this command in a DM"}
```

For an unknown sender, JSON responses gain this field:

```json
{"delivery_warning":"⚠️ This reply was flood-sent because I do not have your advert. Please send an advert for reliable future replies."}
```
