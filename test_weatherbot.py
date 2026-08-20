import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

from meshcore import EventType

import weatherbot


class FakeEvent:
    def __init__(self, event_type, payload=None, attributes=None):
        self.type = event_type
        self.payload = payload or {}
        self.attributes = attributes or {}


def make_config(state_file, **changes):
    values = dict(
        repeater_host="127.0.0.1",
        repeater_port=5001,
        bot_name="WeatherBot",
        weather_channel_index=1,
        weather_channel_name="Weather",
        weather_channel_key="",
        alert_zip_codes=[],
        alert_poll_seconds=60,
        direct_retries=3,
        ack_timeout_seconds=0.001,
        reconnect_seconds=1,
        http_timeout_seconds=1,
        noaa_user_agent="weatherbot-tests (tests@example.com)",
        state_file=Path(state_file),
        log_level="WARNING",
    )
    values.update(changes)
    return weatherbot.BotConfig(**values)


class FakeWeatherService(weatherbot.WeatherService):
    def __init__(self):
        super().__init__("weatherbot-tests (tests@example.com)", timeout=1)
        self.alert_params = None

    async def _get_json(self, url, params=None):
        if "zippopotam" in url:
            return {
                "places": [
                    {
                        "latitude": "41.8858",
                        "longitude": "-87.6181",
                        "place name": "Chicago",
                        "state abbreviation": "IL",
                    }
                ]
            }
        if "/points/" in url:
            return {
                "properties": {
                    "relativeLocation": {
                        "properties": {"city": "Chicago", "state": "IL"}
                    },
                    "observationStations": "https://api.weather.gov/grid/stations",
                    "forecastHourly": "https://api.weather.gov/grid/hourly",
                }
            }
        if url.endswith("/grid/stations"):
            return {"features": [{"id": "https://api.weather.gov/stations/KTEST"}]}
        if url.endswith("/observations/latest"):
            return {
                "properties": {
                    "temperature": {"value": 20, "unitCode": "wmoUnit:degC"},
                    "textDescription": "Partly Cloudy",
                    "relativeHumidity": {"value": 50},
                    "windSpeed": {"value": 16.0934, "unitCode": "wmoUnit:km_h-1"},
                    "windDirection": {"value": 225},
                }
            }
        if "/alerts/active" in url:
            self.alert_params = params
            return {
                "features": [
                    {
                        "id": "alert-1",
                        "properties": {
                            "event": "Heat Advisory",
                            "severity": "Moderate",
                            "sent": "2026-08-20T10:00:00-05:00",
                            "ends": "2026-08-20T20:00:00-05:00",
                        },
                    }
                ]
            }
        raise AssertionError("unexpected URL " + url)


class WeatherFormattingTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_conditions_and_active_alert(self):
        service = FakeWeatherService()
        report = await service.weather_report("60601")
        await service.close()
        self.assertIn("WX 60601 Chicago, IL", report)
        self.assertIn("68F", report)
        self.assertIn("humidity 50%", report)
        self.assertIn("wind SW 10 mph", report)
        self.assertIn("Heat Advisory", report)
        self.assertEqual(service.alert_params, {"point": "41.8858,-87.6181"})

    async def test_utf8_chunks_fit_mesh_limit(self):
        chunks = weatherbot.split_mesh_text("storm warning " * 100 + "☂", 140)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 140 for chunk in chunks))
        self.assertTrue(chunks[0].startswith("[1/"))

    async def test_command_parser_accepts_openhop_channel_sender_label(self):
        self.assertEqual(weatherbot.parse_wx_command("wx 60601"), "60601")
        self.assertEqual(
            weatherbot.parse_wx_command("Alice: wx 60601", channel_message=True),
            "60601",
        )
        self.assertIsNone(weatherbot.parse_wx_command("Alice: wx 60601"))
        self.assertIsNone(weatherbot.parse_wx_command("wx 60601 please"))


class FakeRoutingCommands:
    def __init__(self, ack=b"\x12\x34\x56\x78"):
        self.ack = ack
        self.attempts = []
        self.reset_contacts = []
        self.flood = False

    async def send_msg(self, contact, text, timestamp, attempt):
        self.attempts.append((contact, text, timestamp, attempt))
        return FakeEvent(
            EventType.MSG_SENT,
            {
                "type": 1 if self.flood else 0,
                "expected_ack": self.ack,
                "suggested_timeout": 1,
            },
        )

    async def reset_path(self, contact):
        self.reset_contacts.append(contact)
        self.flood = True
        return FakeEvent(EventType.OK)


class FakeMesh:
    def __init__(self, commands):
        self.commands = commands


class RoutingPolicyTests(unittest.IsolatedAsyncioTestCase):
    def make_bot_and_mesh(self, directory):
        bot = weatherbot.WeatherBot(
            make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
        )
        contact = {
            "public_key": "313233343536" + "00" * 26,
            "adv_name": "Alice",
            "out_path_len": 2,
        }
        bot._contacts["313233343536"] = contact
        commands = FakeRoutingCommands()
        return bot, FakeMesh(commands), contact

    async def test_initial_plus_three_retries_then_one_flood(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, mesh, contact = self.make_bot_and_mesh(directory)
            sent = await bot.send_dm_with_fallback(mesh, "313233343536", "weather")
        self.assertTrue(sent)
        self.assertEqual([item[3] for item in mesh.commands.attempts], [0, 1, 2, 3, 4])
        self.assertEqual(len({item[2] for item in mesh.commands.attempts}), 1)
        self.assertEqual(mesh.commands.reset_contacts, [contact])

    async def test_ack_received_before_wait_prevents_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, mesh, _contact = self.make_bot_and_mesh(directory)
            bot._recent_acks.append(mesh.commands.ack.hex())
            self.assertTrue(
                await bot.send_dm_with_fallback(mesh, "313233343536", "weather")
            )
        self.assertEqual(len(mesh.commands.attempts), 1)
        self.assertEqual(mesh.commands.reset_contacts, [])


class FakeSetupCommands:
    def __init__(self):
        self.calls = []
        self.channel_messages = []
        self.contact = {
            "public_key": "aabbccddeeff" + "00" * 26,
            "adv_name": "Alice",
            "out_path_len": 1,
        }

    async def set_name(self, name):
        self.calls.append(("set_name", name))
        return FakeEvent(EventType.OK)

    async def set_channel(self, index, name, secret):
        self.calls.append(("set_channel", index, name, secret))
        return FakeEvent(EventType.OK)

    async def get_channel(self, index):
        self.calls.append(("get_channel", index))
        return FakeEvent(
            EventType.CHANNEL_INFO,
            {"channel_idx": index, "channel_name": "Weather"},
        )

    async def get_contacts(self):
        self.calls.append(("get_contacts",))
        return FakeEvent(EventType.CONTACTS, {self.contact["public_key"]: self.contact})

    async def send_advert(self, flood=False):
        self.calls.append(("send_advert", flood))
        return FakeEvent(EventType.OK)

    async def send_chan_msg(self, index, text):
        self.channel_messages.append((index, text))
        return FakeEvent(EventType.OK)


class FakeBriefWeather:
    async def weather_report(self, zip_code):
        return f"WX {zip_code}: Clear, 72F. No active NWS alerts."


class MeshAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_uses_meshcore_commands_and_exact_16_byte_key(self):
        with tempfile.TemporaryDirectory() as directory:
            key = bytes(range(16))
            config = make_config(
                Path(directory) / "state.json",
                weather_channel_key=base64.b64encode(key).decode(),
            )
            bot = weatherbot.WeatherBot(config, weather=FakeBriefWeather())
            commands = FakeSetupCommands()
            await bot._prepare_mesh(FakeMesh(commands))
        self.assertIn(("set_channel", 1, "Weather", key), commands.calls)
        self.assertIn(("send_advert", True), commands.calls)
        self.assertIn("aabbccddeeff", bot._contacts)

    async def test_channel_request_replies_to_weather_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(
                make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
            )
            commands = FakeSetupCommands()
            handled = await bot.handle_message(
                FakeMesh(commands),
                weatherbot.InboundMessage("Alice: wx 60601", channel_index=1),
            )
        self.assertTrue(handled)
        self.assertEqual(commands.channel_messages[0][0], 1)
        self.assertIn("WX 60601", commands.channel_messages[0][1])


class FakeAlertWeather:
    async def resolve_zip(self, zip_code):
        return weatherbot.Location(zip_code, 1.0, 2.0, "City", "ST", None, None)

    async def active_alerts(self, location):
        return [
            {
                "id": "same-alert",
                "properties": {
                    "event": "Tornado Warning",
                    "severity": "Extreme",
                    "urgency": "Immediate",
                    "headline": "Take shelter now.",
                    "sent": "2026-08-20T12:00:00Z",
                    "expires": "2026-08-20T13:00:00Z",
                },
            }
        ]


class AlertPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_alerts_are_grouped_by_zip_and_persistently_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            config = make_config(state, alert_zip_codes=["60601", "60602"])
            commands = FakeSetupCommands()
            mesh = FakeMesh(commands)
            bot = weatherbot.WeatherBot(config, weather=FakeAlertWeather())
            self.assertEqual(await bot.poll_alerts(mesh), 1)
            self.assertIn(
                "60601,60602",
                " ".join(text for _index, text in commands.channel_messages),
            )
            first_count = len(commands.channel_messages)
            self.assertEqual(await bot.poll_alerts(mesh), 0)
            self.assertEqual(len(commands.channel_messages), first_count)

            restarted = weatherbot.WeatherBot(config, weather=FakeAlertWeather())
            self.assertEqual(await restarted.poll_alerts(mesh), 0)
            self.assertTrue(json.loads(state.read_text())["seen_alerts"])


if __name__ == "__main__":
    unittest.main()
