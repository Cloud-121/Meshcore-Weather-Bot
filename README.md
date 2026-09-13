# pyMC/openHop Weather Bot

A small Python bot for the
[openHop Repeater](https://github.com/openhop-dev/openhop_repeater). It connects to one
of the repeater's companion TCP ports through the maintained
[`meshcore` Python client](https://github.com/meshcore-dev/meshcore_py); it does not
need `openhop_core` or a separate radio.

It responds to `wx ZIPCODE` in a DM or in `#Weather` with current conditions and a
five-hour forecast from Open-Meteo. `wx help` lists commands and
identifies the bot as a Gulf Coast Mesh boat designed by ScarlettOSA. `wx version`
reports the Git commit currently running. Append `json` to any `wx` command (for
example, `wx 70818 json` or `wx help json`) to receive a compact structured JSON
response instead of the normal text reply.

Current conditions include Open-Meteo's modelled apparent (feels-like) temperature.
The text response labels it `Feels like`; the compact `wx ZIPCODE json` response uses
the `i` key.

For app integrations on the `#wx-bot-hidden` channel, see
[the versioned mesh JSON API](JSON_API.md). It uses
`bot json api` for discovery and `wx ZIPCODE json all api` for compact current and
five-hour forecast data; existing `json` commands remain unchanged.

For smaller machine replies on `#wx-bot-hidden`, see [Compact API v2](API_V2.md). Send
`wx 60601 all api2` for a binary-packed forecast carried as Base64url text, or
`bot api2` for discovery. The reference decoder is in `api_v2.py`. Normal commands
and automatic alerts remain human-readable; existing JSON formats are preserved.

API v2 also provides compact CONUS radar snapshots for map-capable clients. For
example, `wx radar 30.4515 -91.1871 now api2` requests observed reflectivity in a
50-mile circle, while `-5h` through `+5h` select one past observation or future
HRRR simulated-reflectivity forecast. Radar replies are binary packed and capped at
four MeshCore application messages; see [API_V2.md](API_V2.md) for the Flutter-facing
grid and bit-packing contract.

Use `wx report ZIPCODE` in a DM to subscribe that identity to NOAA alerts for a ZIP;
repeat it to add more ZIPs. Every report alert is sent by DM and ends with
`wx report stop`, which removes all of that identity's subscriptions. In `#Weather`,
the report command tells the user to use a DM instead. `ping` works in a DM or in
`#test`, returning `pong`, the bot receipt time, and the best route data the companion
provides. For `#test` packets the bot shows raw route hashes when the companion's RF
log includes a matching packet (for example, `AF-2B-8A` or `AF2B-8A10`); otherwise it
falls back to hop count. DM pings use the reliable hop-count fallback because their
encrypted raw packets cannot be matched safely. When both the bot and a DM sender have
advertised GPS coordinates, `ping` also shows their approximate straight-line distance;
this is not the distance through relay nodes. `ping json` is also available for
structured diagnostic output.

Before a DM reply the bot
refreshes the sender's route from its newest advert path (`get_advert_path`), then uses
that route and waits for the MeshCore ACK. It makes the initial routed attempt plus the
configured three retries. If none is acknowledged, it resets that stale outbound route
and sends the reply once by flood. Repeated or retransmitted copies of the same request
from the same sender for the same ZIP are deduplicated for `request_dedup_seconds`
(default 120) so a retry never triggers a second reply. Channel replies and automatic
alerts are normal encrypted channel floods.

Weather conditions and forecasts come from Open-Meteo's model data. Active
watches/warnings/advisories and `wx report` subscriptions continue to come from the US
National Weather Service. ZIP centroids come from Zippopotam.us because both providers
accept coordinates, not ZIP codes.

## 1. Configure an openHop companion

Add a companion identity to the repeater's `config.yaml` (the dashboard can also do
this):

```yaml
identities:
  companions:
    - name: "WeatherBot"
      identity_key: "YOUR_32_BYTE_IDENTITY_SEED_AS_HEX"
      settings:
        node_name: "WeatherBot"
        tcp_port: 5001
        bind_address: "127.0.0.1"
        tcp_timeout: 0
```

Restart the repeater after adding it. `tcp_timeout: 0` prevents an idle disconnect.
Only one TCP client can use a companion at a time, so give this bot its own companion
and port. Keep the bind address on loopback when the bot runs on the repeater host.

Configure channel index `1` as `Weather` with the same key used by your mesh clients.
You can do that in a companion app/dashboard, or put the Base64 key in
`weather_channel_key` and the bot will configure it at startup. A 16-byte key encodes
to 24 Base64 characters. Leave the value empty to use the channel already stored by
the repeater. The bot verifies the channel name before transmitting.

Configure `api_channel_index` (default `3`) as the normal shared channel
`wx-bot-hidden`. Put its Base64 channel key in `api_channel_key`, or leave it
empty when the channel is already stored by the repeater. Channel API and API v2
requests are accepted only there; human-readable weather commands and alerts remain
on `#Weather`. API requests in DMs are also supported.

Also configure `test_channel_index` as `test` (or set both matching values in
`config.json`). The bot verifies that channel at startup and uses it only for public
`ping`; its encryption key must already be configured on the companion.

## 2. Configure and run

Use Python 3.10 or newer and install the two declared runtime dependencies in a
virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp config.json.example config.json
# Edit the repeater address, channel, ZIPs, and the required NWS User-Agent contact for alerts.
python weatherbot.py --config config.json
```

The `alert_zip_codes` list controls automatic alert monitoring. New active NWS alerts
are combined across matching ZIPs, sent once to `#Weather`, and recorded in
`.weatherbot_state.json` so a restart does not repeat them. The first run sends alerts
that are already active. DM report subscriptions and per-user delivery history live in
the same state file; a new subscription also receives any currently active alert.
Polling cannot be configured below 30 seconds, matching NWS rate-limit guidance.
Use only real five-digit ZIP codes in this list; use `[]` to disable automatic channel
alerts. `message_poll_seconds` (default 2) periodically sends the companion
`CMD_SYNC_NEXT_MESSAGE`, because a wake notification is optional. Set `log_level` to
`DEBUG` to see inbound-message metadata and command detection.

Test the Internet-side lookup without a repeater:

```bash
python weatherbot.py --config config.json --weather 60601
```

Run the unit tests:

```bash
python -m unittest -v
```

## Optional systemd service

Use absolute paths matching your checkout:

```ini
[Unit]
Description=openHop Weather and Alert Bot
After=network-online.target openhop-repeater.service
Wants=network-online.target

[Service]
Type=simple
User=repeater
WorkingDirectory=/opt/pymc-weatherbot
ExecStart=/opt/pymc-weatherbot/.venv/bin/python /opt/pymc-weatherbot/weatherbot.py --config /etc/pymc-weatherbot/config.json
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

The service user needs read access to `config.json` and write access to the directory
containing `state_file`.

## Notes

- US five-digit ZIP codes are supported. ZIP+4 input is accepted and reduced to its
  first five digits.
- Open-Meteo current conditions are modelled 15-minute data, not a nearby station
  observation. The source automatically selects the highest-resolution applicable
  forecast model for the requested location.
- Alert messages are concise and may be split into numbered MeshCore chunks. Always
  follow official local instructions; this bot is not a replacement for NOAA Weather
  Radio or emergency services.
