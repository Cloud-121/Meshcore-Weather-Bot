"""Protocol vectors, packet budgets, and opt-in integration checks."""

import base64
import random
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import api_v2
import radar
import radar_codec
import weatherbot
from test_weatherbot import FakeBriefWeather, FakeMesh, FakeSetupCommands, make_config


def sample():
    return {"k": "w", "z": "00601", "g": 1780000000,
            "n": [68, 2, 50, 225, 10, 77],
            "h": [[i * 60, 68 + i, 2, 225, 10, 10] for i in range(5)],
            "a": [[6, 2]]}


def radar_sample(width=32, forecast=False):
    return {
        "k": "r", "lat": 30.4515, "lon": -91.1871, "o": 3 if forecast else 0,
        "v": 1780000000, "s": "forecast" if forecast else "observed", "n": width,
        "c": [index % 8 for index in range(radar_codec.circle_cell_count(width))],
        **({"i": 1779992800} if forecast else {}),
    }


class FakeRadar:
    def __init__(self):
        self.snapshot = AsyncMock(return_value=radar_sample())


class CodecTests(unittest.TestCase):
    def test_weather_round_trip_and_packet_budget(self):
        data = sample()
        messages = api_v2.encode(data, response_id=b"\x01\x02\x03", flood_warning=True)
        self.assertEqual(len(messages), 1)
        self.assertLessEqual(len(messages[0].encode()), 140)
        self.assertEqual(api_v2.decode(messages), {
            "id": "010203", "flood_warning": True, "data": data})

    def test_weather_randomized_missing_and_extreme_values(self):
        rng = random.Random(12)
        for _ in range(200):
            data = sample()
            rows = [[rng.choice([None, rng.randint(-150, 500)]) for _ in range(6)]
                    for _ in range(6)]
            data.update(n=rows[0], h=rows[1:], a=[[i, i % 5] for i in range(5)], x=True)
            messages = api_v2.encode(data)
            self.assertTrue(all(len(m.encode()) <= 140 for m in messages))
            self.assertEqual(api_v2.decode(messages)["data"], data)

    def test_positional_schemas(self):
        for data in [
            {"type": "discovery", "cmd": 127, "lim": [140, 16], "u": ["F", "mph", "%"]},
            {"type": "version", "git_commit": "abc123"},
            {"type": "report", "status": "enabled", "zip_code": "00601"},
            {"type": "pong", "received_at": "2026-09-12T00:00:00+00:00", "path": "3 hops"},
            {"type": "error", "command": "wx", "error": 1, "zip_code": "00000"},
        ]:
            with self.subTest(data=data):
                self.assertEqual(api_v2.decode(api_v2.encode(data))["data"], data)

    def test_fragment_reordering_duplicates_missing_and_mixed(self):
        rng = random.Random(3)
        value = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(700))
        data = {"type": "version", "git_commit": value}
        messages = api_v2.encode(data, response_id=b"abc")
        self.assertGreater(len(messages), 1)
        self.assertEqual(api_v2.decode(list(reversed(messages)) + messages[:1])["data"], data)
        with self.assertRaises(ValueError):
            api_v2.decode(messages[:-1])
        with self.assertRaises(ValueError):
            api_v2.decode(messages + api_v2.encode(data, response_id=b"def")[:1])

    def test_malformed_and_oversized(self):
        for text in ["", "~W2!", "~W2A", "~W2AA", "~W2" + "A" * 140]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                api_v2.decode([text])
        with self.assertRaises(ValueError):
            api_v2.encode({"type": "version", "git_commit": "a" * 9000})
        with self.assertRaises(ValueError):
            api_v2.decode(["~W2" + base64.urlsafe_b64encode(b"\x01abc\x80").decode().rstrip("=")])

    def test_stable_error_vector(self):
        self.assertEqual(api_v2.encode({"type": "error", "command": "wx", "error": 1},
                                       response_id=b"\x01\x02\x03"),
                         ["~W2BgECA1sid3giLDFd"])

    def test_published_weather_vectors(self):
        self.assertEqual(api_v2.decode([
            "~W2IQECA7vJ0nDq0QU2-w5GlpRDzCKzGHntGYBsIFPEvoKJAQiwUmoA"
        ])["data"], sample())
        empty = sample()
        empty.update(n=[None] * 6, h=[], a=[])
        self.assertEqual(api_v2.decode(["~W2AQECA9kEgMri0AYAAA"])["data"], empty)


    def test_compression_limits_and_conflicting_duplicates(self):
        compressor = zlib.compressobj(wbits=-15)
        payload = compressor.compress(b"a" * (api_v2.MAX_DECODED + 1)) + compressor.flush()
        message = "~W2" + base64.urlsafe_b64encode(b"\x23abc" + payload).decode().rstrip("=")
        with self.assertRaises(ValueError):
            api_v2.decode([message])
        first = api_v2.encode({"type": "version", "git_commit": "a"}, response_id=b"abc")
        second = api_v2.encode({"type": "version", "git_commit": "b"}, response_id=b"abc")
        with self.assertRaises(ValueError):
            api_v2.decode(first + second)

    def test_zero_hours_and_all_nulls(self):
        data = sample()
        data.update(n=[None] * 6, h=[], a=[])
        self.assertEqual(api_v2.decode(api_v2.encode(data))["data"], data)

    def test_radar_round_trip_and_hard_four_message_budget(self):
        rng = random.Random(44)
        for forecast in (False, True):
            data = radar_sample(forecast=forecast)
            data["c"] = [rng.randrange(8) for _ in data["c"]]
            messages = api_v2.encode(data, response_id=b"rad")
            self.assertEqual(len(messages), 4)
            self.assertTrue(all(len(message.encode()) <= 140 for message in messages))
            self.assertEqual(api_v2.decode(list(reversed(messages)))["data"], data)

    def test_radar_codec_rejects_bad_metadata_and_cells(self):
        data = radar_sample()
        for change in ({"o": 6}, {"lat": 91}, {"s": "radar"}, {"c": data["c"][:-1]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                api_v2.encode({**data, **change})


class RadarParserTests(unittest.TestCase):
    def test_radar_request_parser(self):
        self.assertEqual(radar.parse_radar_request("wx radar 30.4515 -91.1871 now"),
                         (30.4515, -91.1871, 0))
        self.assertEqual(radar.parse_radar_request("WX RADAR 30 -91 -5H"),
                         (30.0, -91.0, -5))
        self.assertEqual(radar.parse_radar_request("wx radar 30 -91 +5h"),
                         (30.0, -91.0, 5))
        self.assertIsNone(radar.parse_radar_request("wx radar 30 -91 +6h"))
        with self.assertRaises(radar.RadarError):
            radar.parse_radar_request("wx radar nan -91 now")


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bot = weatherbot.WeatherBot(make_config(Path(self.directory.name) / "state.json"),
                                         weather=FakeBriefWeather(), radar=FakeRadar())
        self.commands = FakeSetupCommands()
        self.mesh = FakeMesh(self.commands)

    async def test_weather_v2_one_message_v1_three_and_text_readable(self):
        for command, count, prefix in [
            ("wx 60601 all api2", 1, "~W2"),
            ("wx 60601 json all api", 3, "{"),
            ("wx 60601", 1, "WX 60601"),
        ]:
            self.commands.channel_messages.clear()
            self.assertTrue(await self.bot.handle_message(
                self.mesh, weatherbot.InboundMessage(command, channel_index=1)))
            self.assertEqual(len(self.commands.channel_messages), count)
            self.assertTrue(self.commands.channel_messages[0][1].startswith(prefix))

    async def test_discovery_version_invalid_and_channel_restrictions(self):
        for command, kind in [("bot api2", "discovery"), ("wx help api2", "discovery"),
                              ("wx version api2", "version"), ("bad api2", "error")]:
            self.commands.channel_messages.clear()
            await self.bot.handle_message(self.mesh, weatherbot.InboundMessage(command, channel_index=1))
            result = api_v2.decode([text for _, text in self.commands.channel_messages])
            self.assertEqual(result["data"]["type"], kind)
        self.assertFalse(await self.bot.handle_message(
            self.mesh, weatherbot.InboundMessage("ping api2", channel_index=1)))

    async def test_dm_warning_subscription_and_ping(self):
        sent = []
        async def send(mesh, prefix, text):
            sent.append(text)
        with patch.object(self.bot, "_has_contact", AsyncMock(return_value=False)), \
                patch.object(self.bot, "send_dm_with_fallback", side_effect=send):
            for command, kind in [("wx report 00601 api2", "report"),
                                  ("wx report stop api2", "report"), ("ping api2", "pong")]:
                sent.clear()
                await self.bot.handle_message(self.mesh, weatherbot.InboundMessage(command, sender_prefix="abcdef123456"))
                result = api_v2.decode(sent)
                self.assertTrue(result["flood_warning"])
                self.assertEqual(result["data"]["type"], kind)

    async def test_weather_error_and_duplicate(self):
        with patch.object(self.bot.weather, "weather_api_all", AsyncMock(side_effect=weatherbot.WeatherError("ZIP code was not found"))):
            message = weatherbot.InboundMessage("wx 00000 all api2", channel_index=1, sender_timestamp=123)
            await self.bot.handle_message(self.mesh, message)
            await self.bot.handle_message(self.mesh, message)
        self.assertEqual(len(self.commands.channel_messages), 1)
        self.assertEqual(api_v2.decode([self.commands.channel_messages[0][1]])["data"]["error"], 1)


    async def test_current_json_compatibility_and_subscription_channel_error(self):
        current = {"z": "00601", "l": "Example, PR", "t": -2, "c": "Clear", "h": 0}
        with patch.object(self.bot.weather, "weather_json", AsyncMock(return_value=current), create=True):
            for command in ["wx 00601 api2", "wx 00601 json"]:
                self.commands.channel_messages.clear()
                await self.bot.handle_message(self.mesh, weatherbot.InboundMessage(command, channel_index=1))
                texts = [text for _, text in self.commands.channel_messages]
                if command.endswith("api2"):
                    result = api_v2.decode(texts)["data"]
                    for key, value in current.items():
                        self.assertEqual(result[key], value)
                else:
                    self.assertTrue(texts[0].startswith("{"))
        self.commands.channel_messages.clear()
        await self.bot.handle_message(self.mesh, weatherbot.InboundMessage("wx report 00601 api2", channel_index=1))
        self.assertEqual(api_v2.decode([self.commands.channel_messages[0][1]])["data"]["error"], 4)

    async def test_radar_request_response_and_duplicate(self):
        message = weatherbot.InboundMessage(
            "wx radar 30.4515 -91.1871 +3h api2", channel_index=1,
            sender_timestamp=123,
        )
        await self.bot.handle_message(self.mesh, message)
        await self.bot.handle_message(self.mesh, message)
        texts = [text for _, text in self.commands.channel_messages]
        self.assertLessEqual(len(texts), 4)
        self.assertEqual(api_v2.decode(texts)["data"], radar_sample())
        self.bot.radar.snapshot.assert_awaited_once_with(30.4515, -91.1871, 3)

    async def test_invalid_radar_request_is_compact_error(self):
        await self.bot.handle_message(
            self.mesh,
            weatherbot.InboundMessage("wx radar 95 -91 now api2", channel_index=1),
        )
        result = api_v2.decode([text for _, text in self.commands.channel_messages])
        self.assertEqual(result["data"], {"type": "error", "command": "radar", "error": 5})


if __name__ == "__main__":
    unittest.main()
