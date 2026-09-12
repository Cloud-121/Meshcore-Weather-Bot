"""Compare complete v1/v2 mesh replies: python benchmark_api_v2.py."""

import json
import random
import zlib

import api_v2
import radar_codec
from weatherbot import _compact_api_object, api_mesh_envelopes, api_weather_parts


def scenarios():
    for name, temperature, alerts in [
        ("normal", 68, []),
        ("five alerts", 105, [["Heat", "Extreme"]] * 5),
        ("freezing", -25, [["Winter", "Severe"]]),
        ("missing values", None, []),
    ]:
        yield name, {
            "k": "w", "z": "00601", "g": 1780000000,
            "n": {"t": temperature, "c": "Partly cloudy", "h": 50, "w": "SW 10 mph", "i": None},
            "h": [{"m": i * 60, "t": temperature, "c": "Partly cloudy",
                   "w": "SW 10 mph", "p": None} for i in range(5)],
            "a": alerts,
        }


def main():
    print("Scenario          v1 bytes/msgs  v2 bytes/msgs  JSON / zlib JSON bytes*")
    for name, source in scenarios():
        compact, clipped = _compact_api_object(source)
        if clipped:
            compact["x"] = True
        v1 = api_mesh_envelopes(api_weather_parts(source))
        v2 = api_v2.encode(compact, response_id=b"\x01\x02\x03")
        assert api_v2.decode(v2)["data"] == compact
        raw = json.dumps(compact, separators=(",", ":")).encode()
        print(f"{name:17} {sum(len(m.encode()) for m in v1):4}/{len(v1):<9} "
              f"{sum(len(m.encode()) for m in v2):4}/{len(v2):<9} {len(raw):4} / {len(zlib.compress(raw, 9)):4}")
    rng = random.Random(44)
    radar = {
        "k": "r", "lat": 30.4515, "lon": -91.1871, "o": 5,
        "v": 1780000000, "i": 1779992800, "s": "forecast", "n": 32,
        "c": [rng.randrange(8) for _ in range(radar_codec.circle_cell_count(32))],
    }
    messages = api_v2.encode(radar, response_id=b"\x01\x02\x03")
    assert len(messages) == 4 and api_v2.decode(messages)["data"] == radar
    print(f"radar worst-case {'n/a':>10}    "
          f"{sum(len(m.encode()) for m in messages):4}/{len(messages):<9} binary packed")
    print("* JSON comparison excludes framing and binary-to-text overhead.")


if __name__ == "__main__":
    main()
