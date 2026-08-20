# pyMC/openHop Weather Bot

A small Python bot for the
[openHop Repeater](https://github.com/openhop-dev/openhop_repeater). It connects to one
of the repeater's companion TCP ports through the maintained
[`meshcore` Python client](https://github.com/meshcore-dev/meshcore_py); it does not
need `openhop_core` or a separate radio.

It responds to exactly `wx ZIPCODE` in a DM or in `#Weather`. A DM reply uses the
sender contact's stored route and waits for the MeshCore ACK. It makes the initial
routed attempt plus the configured three retries. If none is acknowledged, it resets
that stale outbound route and sends the reply once by flood. Channel replies and
automatic alerts are normal encrypted channel floods.

Weather observations and active watches/warnings/advisories come from the US National
Weather Service. ZIP centroids come from Zippopotam.us because `api.weather.gov`
accepts coordinates, not ZIP codes.

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

## 2. Configure and run

Use Python 3.10 or newer and install the two declared runtime dependencies in a
virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp config.json.example config.json
# Edit the repeater address, channel, ZIPs, and the required NWS User-Agent contact.
python weatherbot.py --config config.json
```

The `alert_zip_codes` list controls automatic alert monitoring. New active NWS alerts
are combined across matching ZIPs, sent once to `#Weather`, and recorded in
`.weatherbot_state.json` so a restart does not repeat them. The first run sends alerts
that are already active. Polling cannot be configured below 30 seconds, matching NWS
rate-limit guidance.

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
Description=openHop NOAA Weather Bot
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
- NWS observations can be delayed; when the nearest station is unavailable the bot
  clearly labels the NWS current-hour forecast fallback.
- Alert messages are concise and may be split into numbered MeshCore chunks. Always
  follow official local instructions; this bot is not a replacement for NOAA Weather
  Radio or emergency services.
