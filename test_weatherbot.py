import asyncio
import base64
import json
import tempfile
import time
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
        test_channel_index=2,
        test_channel_name="test",
        alert_zip_codes=[],
        alert_poll_seconds=60,
        message_poll_seconds=2,
        direct_retries=3,
        ack_timeout_seconds=0.001,
        reconnect_seconds=1,
        request_dedup_seconds=120,
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
                    "heatIndex": {"value": 25, "unitCode": "wmoUnit:degC"},
                    "windSpeed": {"value": 16.0934, "unitCode": "wmoUnit:km_h-1"},
                    "windDirection": {"value": 225},
                }
            }
        if url.endswith("/grid/hourly"):
            return {
                "properties": {
                    "periods": [
                        {
                            "startTime": f"2026-08-20T{hour:02d}:00:00-05:00",
                            "temperature": 68 + index,
                            "temperatureUnit": "F",
                            "shortForecast": "Partly Cloudy",
                            "windDirection": "SW",
                            "windSpeed": "10 mph",
                            "probabilityOfPrecipitation": {"value": 10},
                        }
                        for index, hour in enumerate(range(12, 17))
                    ]
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
                            "timeZone": "America/Chicago",
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
        self.assertIn("☀️ Chicago, IL 60601", report)
        self.assertIn("68°F", report)
        self.assertIn("☀️ Heat index 77°F", report)
        self.assertIn("💧 50%", report)
        self.assertIn("💨 SW 10 mph", report)
        self.assertIn("⚠️ Heat Advisory", report)
        self.assertEqual(service.alert_params, {"point": "41.8858,-87.6181"})

    async def test_weather_json_is_a_compact_text_summary(self):
        service = FakeWeatherService()
        report = await service.weather_json("60601")
        await service.close()
        self.assertEqual(
            report,
            {
                "z": "60601",
                "l": "Chicago, IL",
                "t": 68,
                "c": "Partly Cloudy",
                "h": 50,
                "i": 77,
                "w": "SW 10 mph",
                "a": [["Heat Advisory", "Moderate", "Aug 20 8:00 PM CDT"]],
            },
        )

    async def test_weather_api_all_has_five_hourly_periods(self):
        service = FakeWeatherService()
        report = await service.weather_api_all("60601")
        await service.close()
        self.assertEqual(report["k"], "w")
        self.assertEqual(len(report["h"]), 5)
        self.assertEqual(report["h"][0]["t"], 68)
        self.assertEqual(report["n"]["i"], 77)
        self.assertEqual(report["a"][0][:2], ["Heat Advisory", "Moderate"])

    async def test_report_lines_fit_mesh_limit(self):
        service = FakeWeatherService()
        report = await service.weather_report("60601")
        await service.close()
        chunks = weatherbot.split_mesh_text(report, 140)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 140 for chunk in chunks))

    async def test_split_mesh_text_preserves_line_breaks(self):
        chunks = weatherbot.split_mesh_text("☀️ Line one\n💧 Line two", 140)
        self.assertGreaterEqual(len(chunks), 1)
        self.assertIn("\n", chunks[0])

    async def test_split_mesh_text_reconstructs_long_text(self):
        text = "\n".join(
            f"☀️ Line {index} with some padding words here" for index in range(30)
        )
        chunks = weatherbot.split_mesh_text(text, 140)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.encode("utf-8")) <= 140 for c in chunks))
        joined = " ".join(chunk.split(" ", 1)[1] for chunk in chunks)
        self.assertEqual(" ".join(joined.split()), " ".join(text.split()))

    async def test_split_mesh_text_keeps_lines_intact(self):
        lines = ["Line one is short", "Line two is short too", "Line three here"]
        text = "\n".join(lines)
        chunks = weatherbot.split_mesh_text(text, 140)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], text)

    async def test_format_channel_alert_keeps_full_description_and_caps(self):
        long_description = "Description sentence. " * 300
        alert = {
            "id": "a1",
            "properties": {
                "event": "Heat Advisory",
                "severity": "Moderate",
                "urgency": "Expected",
                "timeZone": "America/Chicago",
                "headline": "Short headline",
                "description": long_description,
                "instruction": "Drink water.",
                "sent": "2026-08-20T10:00:00-05:00",
                "expires": "2026-08-20T20:00:00-05:00",
            },
        }
        message = weatherbot.format_channel_alert(alert, ["60601", "60602"])
        body = message.split("\n", 2)[2]
        self.assertEqual(len(body), 2000)
        self.assertTrue(message.startswith("🚨 NWS ALERT: 60601,60602"))
        self.assertTrue(body.startswith("Description sentence."))

    async def test_format_channel_alert_appends_instruction_when_short(self):
        alert = {
            "id": "a3",
            "properties": {
                "event": "Heat Advisory",
                "severity": "Moderate",
                "urgency": "Expected",
                "description": "A short warning body.",
                "instruction": "Drink water.",
            },
        }
        message = weatherbot.format_channel_alert(alert, ["60601"])
        self.assertIn("A short warning body. Drink water.", message)

    async def test_format_channel_alert_uses_description_over_headline(self):
        alert = {
            "id": "a2",
            "properties": {
                "event": "Flood Watch",
                "severity": "Severe",
                "urgency": "Expected",
                "description": "The full detailed warning body text.",
                "headline": "A short headline",
            },
        }
        message = weatherbot.format_channel_alert(alert, ["60601"])
        self.assertIn("The full detailed warning body text.", message)

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


class ConfigurationTests(unittest.TestCase):
    def test_message_poll_defaults_and_legacy_setting_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "noaa_user_agent": "weatherbot-tests (tests@example.com)",
                        "alert_zip_codes": [],
                        "companion_poll_seconds": 0,
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(
                hasattr(weatherbot.load_config(path), "companion_poll_seconds")
            )
            self.assertEqual(weatherbot.load_config(path).message_poll_seconds, 2)

            path.write_text(
                json.dumps(
                    {
                        "noaa_user_agent": "weatherbot-tests (tests@example.com)",
                        "message_poll_seconds": 0,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "message_poll_seconds"):
                weatherbot.load_config(path)


class FakeRoutingCommands:
    def __init__(self, ack=b"\x12\x34\x56\x78", advert_path=None):
        self.ack = ack
        self.attempts = []
        self.reset_contacts = []
        self.path_changes = []
        self.advert_path_calls = []
        self.advert_path = advert_path
        self.flood = False

    async def send_msg(self, contact, text, timestamp, attempt):
        self.attempts.append((contact, text, timestamp, attempt))
        return FakeEvent(
            EventType.MSG_SENT,
            {
                "type": 1 if self.flood or isinstance(contact, str) else 0,
                "expected_ack": self.ack,
                "suggested_timeout": 1,
            },
        )

    async def get_contacts(self):
        return FakeEvent(EventType.CONTACTS, {})

    async def get_advert_path(self, contact):
        self.advert_path_calls.append(contact)
        if self.advert_path is None:
            return FakeEvent(EventType.ERROR, {"reason": "no_path"})
        return FakeEvent(EventType.ADVERT_PATH, self.advert_path)

    async def change_contact_path(self, contact, path, path_hash_mode=None):
        self.path_changes.append((contact, path, path_hash_mode))
        return FakeEvent(EventType.OK)

    async def reset_path(self, contact):
        self.reset_contacts.append(contact)
        self.flood = True
        return FakeEvent(EventType.OK)


class FakeMesh:
    def __init__(self, commands):
        self.commands = commands
        self.decrypt_channel_logs = False

    def set_decrypt_channel_logs(self, enabled):
        self.decrypt_channel_logs = enabled


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

    async def test_advert_path_is_fetched_and_applied_before_send(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, mesh, contact = self.make_bot_and_mesh(directory)
            mesh.commands.advert_path = {
                "path": "aabbcc",
                "path_len": 2,
                "path_hash_mode": 0,
                "timestamp": 123,
            }
            self.assertTrue(
                await bot.send_dm_with_fallback(mesh, "313233343536", "weather")
            )
        self.assertEqual(mesh.commands.advert_path_calls, [contact])
        self.assertEqual(mesh.commands.path_changes, [(contact, "aabbcc", 0)])

    async def test_no_advert_path_leaves_stored_route_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, mesh, contact = self.make_bot_and_mesh(directory)
            mesh.commands.advert_path = {
                "path": "",
                "path_len": -1,
                "path_hash_mode": -1,
            }
            self.assertTrue(
                await bot.send_dm_with_fallback(mesh, "313233343536", "weather")
            )
        self.assertEqual(mesh.commands.advert_path_calls, [contact])
        self.assertEqual(mesh.commands.path_changes, [])

    async def test_duplicate_dm_is_not_answered_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, mesh, _contact = self.make_bot_and_mesh(directory)
            message = weatherbot.InboundMessage(
                "wx 60601", sender_prefix="313233343536", sender_timestamp=100
            )
            bot._recent_acks.append(mesh.commands.ack.hex())
            self.assertTrue(await bot.handle_message(mesh, message))
            self.assertEqual(len(mesh.commands.attempts), 1)

            bot._recent_acks.append(mesh.commands.ack.hex())
            self.assertTrue(await bot.handle_message(mesh, message))
            self.assertEqual(len(mesh.commands.attempts), 1)

            resent = weatherbot.InboundMessage(
                "wx 60601", sender_prefix="313233343536", sender_timestamp=101
            )
            bot._recent_acks.append(mesh.commands.ack.hex())
            self.assertTrue(await bot.handle_message(mesh, resent))
            self.assertEqual(len(mesh.commands.attempts), 2)

    async def test_unknown_dm_sender_receives_flood_reply_and_advert_request(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(
                make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
            )
            mesh = FakeMesh(FakeRoutingCommands())
            message = weatherbot.InboundMessage(
                "wx 60601", sender_prefix="aabbccddeeff"
            )
            self.assertTrue(await bot.handle_message(mesh, message))

        attempts = mesh.commands.attempts
        self.assertGreaterEqual(len(attempts), 1)
        self.assertTrue(all(item[0] == "aabbccddeeff" for item in attempts))
        self.assertIn("Please send an advert", "".join(item[1] for item in attempts))
        self.assertEqual(mesh.commands.advert_path_calls, [])

    async def test_known_dm_reply_does_not_include_unknown_sender_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, mesh, _contact = self.make_bot_and_mesh(directory)
            bot._recent_acks.append(mesh.commands.ack.hex())
            self.assertTrue(
                await bot.handle_message(
                    mesh,
                    weatherbot.InboundMessage(
                        "wx 60601", sender_prefix="313233343536"
                    ),
                )
            )
        self.assertNotIn("Please send an advert", mesh.commands.attempts[0][1])

    async def test_dedup_window_expiry_allows_new_request(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, mesh, _contact = self.make_bot_and_mesh(directory)
            message = weatherbot.InboundMessage(
                "wx 60601", sender_prefix="313233343536", sender_timestamp=100
            )
            bot._recent_acks.append(mesh.commands.ack.hex())
            self.assertTrue(await bot.handle_message(mesh, message))
            self.assertEqual(len(mesh.commands.attempts), 1)

            key = bot._request_key(message)
            bot._seen_requests[key] = time.monotonic() - 1

            bot._recent_acks.append(mesh.commands.ack.hex())
            self.assertTrue(await bot.handle_message(mesh, message))
            self.assertEqual(len(mesh.commands.attempts), 2)

    async def test_duplicate_channel_request_is_not_answered_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(
                make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
            )
            commands = FakeSetupCommands()
            mesh = FakeMesh(commands)
            message = weatherbot.InboundMessage(
                "Alice: wx 60601", channel_index=1, sender_timestamp=100
            )
            self.assertTrue(await bot.handle_message(mesh, message))
            self.assertTrue(await bot.handle_message(mesh, message))
            self.assertEqual(len(commands.channel_messages), 1)


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

    async def send_appstart(self):
        self.calls.append(("send_appstart",))
        return FakeEvent(EventType.SELF_INFO, {"adv_lat": 30.0, "adv_lon": -90.0})

    async def set_channel(self, index, name, secret):
        self.calls.append(("set_channel", index, name, secret))
        return FakeEvent(EventType.OK)

    async def get_channel(self, index):
        self.calls.append(("get_channel", index))
        return FakeEvent(
            EventType.CHANNEL_INFO,
            {
                "channel_idx": index,
                "channel_name": "Weather" if index == 1 else "test",
            },
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

    async def weather_api_all(self, zip_code):
        return {
            "k": "w", "z": zip_code, "g": 1780000000,
            "n": {"t": 72, "c": "Clear", "h": 50, "w": "SW 10 mph"},
            "h": [{"m": hour * 60, "t": 72, "c": "Clear", "w": "SW 10 mph", "p": 0} for hour in range(5)],
            "a": [],
        }


class FakeQueuedMessageCommands:
    def __init__(self, events):
        self.events = list(events)
        self.get_msg_calls = 0

    async def get_msg(self):
        self.get_msg_calls += 1
        return self.events.pop(0)


class FakeQueuedMessageMesh:
    def __init__(self, events):
        self.commands = FakeQueuedMessageCommands(events)
        self.connection_manager = type("Connection", (), {"is_connected": True})()


class MeshAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_sync_drains_until_no_more_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(
                make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
            )
            mesh = FakeQueuedMessageMesh(
                [
                    FakeEvent(EventType.CONTACT_MSG_RECV),
                    FakeEvent(EventType.CHANNEL_MSG_RECV),
                    FakeEvent(EventType.NO_MORE_MSGS),
                ]
            )
            await bot._drain_messages(mesh)

        self.assertEqual(mesh.commands.get_msg_calls, 3)

    async def test_message_poll_syncs_when_no_wake_notification_arrives(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(
                make_config(
                    Path(directory) / "state.json", message_poll_seconds=0.001
                ),
                weather=FakeBriefWeather(),
            )
            disconnected = asyncio.Event()
            calls = 0

            async def drain(_mesh):
                nonlocal calls
                calls += 1
                disconnected.set()

            bot._drain_messages = drain
            await bot._message_poll_loop(object(), disconnected)

        self.assertEqual(calls, 1)

    async def test_raw_test_channel_path_is_attached_only_on_exact_match(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(
                make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
            )
            bot._remember_raw_channel_path(100, "ping", "af2b8a10", 1)
            matched = bot._attach_raw_channel_path(
                weatherbot.InboundMessage(
                    "Alice: ping",
                    channel_index=2,
                    sender_timestamp=100,
                    path_len=2,
                )
            )
            unmatched = bot._attach_raw_channel_path(
                weatherbot.InboundMessage(
                    "Alice: ping",
                    channel_index=2,
                    sender_timestamp=101,
                    path_len=2,
                )
            )

        self.assertEqual(matched.path, "af2b8a10")
        self.assertEqual(matched.path_hash_mode, 1)
        self.assertIsNone(unmatched.path)
        self.assertEqual(weatherbot.route_description(unmatched), "2 hops")

    async def test_direct_distance_requires_known_nondefault_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(
                make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
            )
            bot._bot_coordinates = (30.0, -90.0)
            bot._contacts["aabbccddeeff"] = {
                "public_key": "aabbccddeeff" + "00" * 26,
                "adv_lat": 30.1,
                "adv_lon": -90.0,
            }
            message = weatherbot.InboundMessage("ping", sender_prefix="aabbccddeeff")
            distance = bot._direct_distance_miles(message)
            bot._contacts["aabbccddeeff"]["adv_lat"] = 0.0
            bot._contacts["aabbccddeeff"]["adv_lon"] = 0.0
            missing = bot._direct_distance_miles(message)

        self.assertIsNotNone(distance)
        self.assertAlmostEqual(distance, 6.9, places=1)
        self.assertIsNone(missing)

    async def test_safe_handler_logs_inbound_metadata_without_message_text(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(
                make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
            )
            with self.assertLogs("weatherbot", "DEBUG") as captured:
                await bot._safe_handle_message(
                    FakeMesh(FakeSetupCommands()),
                    weatherbot.InboundMessage("not a command", channel_index=1),
                )

        output = "\n".join(captured.output)
        self.assertIn("Received channel message on channel 1", output)
        self.assertIn("command=no", output)
        self.assertNotIn("not a command", output)

    async def test_setup_uses_meshcore_commands_and_exact_16_byte_key(self):
        with tempfile.TemporaryDirectory() as directory:
            key = bytes(range(16))
            config = make_config(
                Path(directory) / "state.json",
                weather_channel_key=base64.b64encode(key).decode(),
            )
            bot = weatherbot.WeatherBot(config, weather=FakeBriefWeather())
            commands = FakeSetupCommands()
            mesh = FakeMesh(commands)
            await bot._prepare_mesh(mesh)
        self.assertIn(("set_channel", 1, "Weather", key), commands.calls)
        self.assertIn(("send_advert", True), commands.calls)
        self.assertIn("aabbccddeeff", bot._contacts)
        self.assertTrue(mesh.decrypt_channel_logs)

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

    async def test_api_weather_request_sends_three_machine_json_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(
                make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
            )
            commands = FakeSetupCommands()
            mesh = FakeMesh(commands)
            self.assertTrue(
                await bot.handle_message(
                    mesh, weatherbot.InboundMessage("wx 60601 json all api", channel_index=1)
                )
            )
        self.assertEqual(len(commands.channel_messages), 3)
        envelopes = [json.loads(text) for _index, text in commands.channel_messages]
        self.assertTrue(all(envelope["d"].get("k") != "wx" for envelope in envelopes))
        self.assertTrue(all(len(text.encode("utf-8")) <= 140 for _index, text in commands.channel_messages))


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
                    "timeZone": "America/New_York",
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


class CommandFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_subscriptions_persist_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            bot = weatherbot.WeatherBot(
                make_config(state), weather=FakeBriefWeather()
            )
            contact = {
                "public_key": "313233343536" + "00" * 26,
                "out_path_len": -1,
            }
            bot._contacts["313233343536"] = contact
            commands = FakeRoutingCommands()
            commands.flood = True
            mesh = FakeMesh(commands)

            self.assertTrue(
                await bot.handle_message(
                    mesh,
                    weatherbot.InboundMessage(
                        "wx report 70818", sender_prefix="313233343536"
                    ),
                )
            )
            self.assertEqual(bot._report_subscriptions["313233343536"], ["70818"])
            restarted = weatherbot.WeatherBot(
                make_config(state), weather=FakeBriefWeather()
            )
            self.assertEqual(restarted._report_subscriptions["313233343536"], ["70818"])

            restarted._contacts["313233343536"] = contact
            self.assertTrue(
                await restarted.handle_message(
                    mesh,
                    weatherbot.InboundMessage(
                        "wx report stop", sender_prefix="313233343536"
                    ),
                )
            )
            self.assertNotIn("313233343536", restarted._report_subscriptions)

    async def test_personal_alert_is_sent_once_with_stop_instruction(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            bot = weatherbot.WeatherBot(
                make_config(state), weather=FakeAlertWeather()
            )
            bot._report_subscriptions["313233343536"] = ["60601"]
            bot._contacts["313233343536"] = {
                "public_key": "313233343536" + "00" * 26,
                "out_path_len": -1,
            }
            commands = FakeRoutingCommands()
            commands.flood = True
            mesh = FakeMesh(commands)
            self.assertEqual(await bot.poll_alerts(mesh), 1)
            self.assertIn(
                "To stop these alerts: wx report stop",
                "".join(item[1] for item in commands.attempts),
            )
            sent = len(commands.attempts)
            self.assertEqual(await bot.poll_alerts(mesh), 0)
            self.assertEqual(len(commands.attempts), sent)

    async def test_ping_and_json_helpers(self):
        message = weatherbot.InboundMessage(
            "ping",
            channel_index=2,
            path="aabbccdd",
            path_hash_mode=0,
            received_at=weatherbot.datetime(2026, 8, 24, 12, 0, tzinfo=weatherbot.ZoneInfo("UTC")),
        )
        self.assertEqual(
            weatherbot.format_ping_response(message),
            "🏓 Pong\n"
            "Received: 2026-08-24T12:00:00+00:00\n"
            "Path: AA-BB-CC-DD",
        )
        wide_path = weatherbot.InboundMessage("ping", path="af2b8a10", path_hash_mode=1)
        self.assertEqual(weatherbot.route_description(wide_path), "AF2B-8A10")
        distance_message = weatherbot.InboundMessage("ping", approx_direct_miles=12.34)
        self.assertEqual(
            weatherbot.format_ping_response(distance_message).splitlines()[-1],
            "Approx. direct distance: 12.3 mi",
        )
        self.assertEqual(weatherbot.ping_response_data(distance_message)["approx_direct_miles"], 12.3)
        encoded = {"type": "test", "message": "☀" * 100}
        chunks = weatherbot.split_mesh_json(encoded)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 140 for chunk in chunks))
        self.assertEqual(json.loads("".join(chunks)), encoded)
        self.assertEqual(weatherbot.parse_wx_request("wx 70818 json"), ("70818", True))
        self.assertEqual(weatherbot.help_response_data()["type"], "help")
        self.assertRegex(weatherbot.git_commit(), r"^[0-9a-f]{40}$|^unknown$")

    async def test_api_fragments_are_valid_json_and_fit_mesh_limit(self):
        payload = {
            "k": "w",
            "z": "60601",
            "g": 1780000000,
            "n": {"t": 68, "c": "Partly Cloudy", "h": 50, "w": "SW 10 mph", "i": 77},
            "h": [
                {"m": hour * 60, "t": 68, "c": "Partly Cloudy", "w": "SW 10 mph", "p": 10}
                for hour in range(5)
            ],
            "a": [["Heat Advisory", "Moderate", "long end time"]],
        }
        parts = weatherbot.api_weather_parts(payload)
        messages = weatherbot.api_mesh_envelopes(parts)
        self.assertEqual(len(messages), 3)
        envelopes = [json.loads(message) for message in messages]
        self.assertTrue(all(len(message.encode("utf-8")) <= 140 for message in messages))
        self.assertTrue(all(envelope["v"] == 1 for envelope in envelopes))
        self.assertEqual([envelope["p"] for envelope in envelopes], [1, 2, 3])
        merged = {}
        for envelope in envelopes:
            for key, value in envelope["d"].items():
                if key == "h":
                    merged.setdefault("h", []).extend(value)
                else:
                    merged[key] = value
        self.assertEqual(len(merged["h"]), 5)
        self.assertEqual(merged["a"], [[6, 2]])
        self.assertEqual(merged["n"], [68, 2, 50, 225, 10, 77])

    async def test_api_command_parsers(self):
        self.assertTrue(weatherbot.BOT_API_COMMAND.fullmatch("bot json api"))
        self.assertEqual(weatherbot.WX_ALL_API_COMMAND.fullmatch("wx 70818 JSON ALL API").group(1), "70818")


if __name__ == "__main__":
    unittest.main()
