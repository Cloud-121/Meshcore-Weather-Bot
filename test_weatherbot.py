import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
        api_channel_index=3,
        api_channel_name="wx-bot-hidden",
        api_channel_key="",
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
        self.forecast_params = None

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
        if "api.open-meteo.com" in url:
            self.forecast_params = params
            return {
                "current": {
                    "temperature_2m": 68,
                    "apparent_temperature": 77,
                    "relative_humidity_2m": 50,
                    "weather_code": 2,
                    "wind_speed_10m": 10,
                    "wind_direction_10m": 225,
                },
                "hourly": {
                    "time": [1780000000 + hour * 3600 for hour in range(5)],
                    "temperature_2m": [68 + hour for hour in range(5)],
                    "weather_code": [2] * 5,
                    "wind_speed_10m": [10] * 5,
                    "wind_direction_10m": [225] * 5,
                    "precipitation_probability": [10] * 5,
                },
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
    async def test_open_meteo_current_conditions_without_alerts(self):
        service = FakeWeatherService()
        report = await service.weather_report("60601")
        await service.close()
        self.assertIn("☀️ Chicago, IL 60601", report)
        self.assertIn("68°F", report)
        self.assertIn("☀️ Feels like 77°F", report)
        self.assertIn("💧 50%", report)
        self.assertIn("💨 SW 10 mph", report)
        self.assertNotIn("NWS alert", report)
        self.assertEqual(service.forecast_params["temperature_unit"], "fahrenheit")
        self.assertEqual(service.forecast_params["wind_speed_unit"], "mph")

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
                "c": "Partly cloudy",
                "h": 50,
                "i": 77,
                "w": "SW 10 mph",
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
        self.assertEqual(report["a"], [])

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


class FakeScopeCommands:
    def __init__(self):
        self.scope = None
        self.scope_calls = []
        self.sent_scopes = []

    async def set_flood_scope(self, scope):
        self.scope_calls.append(scope)
        self.scope = scope
        return FakeEvent(EventType.OK)


class FakeRoutingCommands(FakeScopeCommands):
    def __init__(self, ack=b"\x12\x34\x56\x78", advert_path=None):
        super().__init__()
        self.ack = ack
        self.attempts = []
        self.reset_contacts = []
        self.path_changes = []
        self.advert_path_calls = []
        self.advert_path = advert_path
        self.flood = False

    async def send_msg(self, contact, text, timestamp, attempt):
        self.sent_scopes.append(self.scope)
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
        self.assertEqual(mesh.commands.sent_scopes, ["#us-la-msy"] * 5)
        self.assertEqual(mesh.commands.scope_calls, ["#us-la-msy", None])

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
                "Alice: wx 60601", channel_index=1, sender_timestamp=100,
                required_region_match=True,
            )
            self.assertTrue(await bot.handle_message(mesh, message))
            sent = list(commands.channel_messages)
            self.assertTrue(await bot.handle_message(mesh, message))
            self.assertEqual(commands.channel_messages, sent)
            self.assertNotIn('Missing "us-la-msy" region.', " ".join(text for _, text in sent))


class FakeSetupCommands(FakeScopeCommands):
    def __init__(self, path_hash_set_event=None, path_hash_mode=1):
        super().__init__()
        self.calls = []
        self.channel_messages = []
        self.path_hash_set_event = path_hash_set_event or FakeEvent(EventType.OK)
        self.path_hash_mode = path_hash_mode
        self.contact = {
            "public_key": "aabbccddeeff" + "00" * 26,
            "adv_name": "Alice",
            "out_path_len": 1,
        }

    async def set_name(self, name):
        self.calls.append(("set_name", name))
        return FakeEvent(EventType.OK)

    async def set_path_hash_mode(self, mode):
        self.calls.append(("set_path_hash_mode", mode))
        return self.path_hash_set_event

    async def get_path_hash_mode(self):
        self.calls.append(("get_path_hash_mode",))
        return self.path_hash_mode

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
                "channel_name": (
                    "Weather" if index == 1 else "wx-bot-hidden" if index == 3 else "test"
                ),
            },
        )

    async def get_contacts(self):
        self.calls.append(("get_contacts",))
        return FakeEvent(EventType.CONTACTS, {self.contact["public_key"]: self.contact})

    async def send_advert(self, flood=False):
        self.calls.append(("send_advert", flood))
        return FakeEvent(EventType.OK)

    async def send_chan_msg(self, index, text):
        self.sent_scopes.append(self.scope)
        self.channel_messages.append((index, text))
        return FakeEvent(EventType.OK)


class OutgoingScopeTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, directory):
        return weatherbot.WeatherBot(
            make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
        )

    async def test_alternating_api_and_regional_channel_sends(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            commands = FakeSetupCommands()
            mesh = FakeMesh(commands)
            await bot.send_channel(mesh, "weather")
            await bot.send_channel(mesh, "api", 3, api=True)
            await bot.send_channel(mesh, "pong", 2)
            self.assertEqual(commands.sent_scopes, ["#us-la-msy", "*", "#us-la-msy"])
            self.assertEqual(commands.scope_calls, ["#us-la-msy", None, "*", None, "#us-la-msy", None])
            self.assertIsNone(commands.scope)

    async def test_unknown_and_missing_route_dm_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            for known in (False, True):
                for api in (False, True):
                    with self.subTest(known=known, api=api):
                        bot = self.make_bot(directory)
                        commands = FakeRoutingCommands()
                        commands.flood = True
                        if known:
                            bot._contacts["aabbccddeeff"] = {
                                "public_key": "aabbccddeeff" + "00" * 26,
                                "out_path_len": -1,
                            }
                        self.assertTrue(await bot.send_dm_with_fallback(
                            FakeMesh(commands), "aabbccddeeff", "reply", api=api,
                        ))
                        scope = "*" if api else "#us-la-msy"
                        self.assertEqual(commands.sent_scopes, [scope])
                        self.assertEqual(commands.scope_calls, [scope, None])

    async def test_api_dm_reply_paths_select_unscoped(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            commands = FakeRoutingCommands()
            mesh = FakeMesh(commands)
            for command in ("bot json api", "bot api2", "wx 60601 all api2"):
                with self.subTest(command=command):
                    commands.sent_scopes.clear()
                    self.assertTrue(await bot.handle_message(
                        mesh, weatherbot.InboundMessage(command, sender_prefix="aabbccddeeff"),
                    ))
                    self.assertTrue(commands.sent_scopes)
                    self.assertEqual(set(commands.sent_scopes), {"*"})
                    self.assertIsNone(commands.scope)

    async def test_scope_selection_failure_prevents_send(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            commands = FakeSetupCommands()
            commands.set_flood_scope = AsyncMock(side_effect=[
                FakeEvent(EventType.ERROR, {"reason": "unsupported"}),
                FakeEvent(EventType.OK),
            ])
            with self.assertRaisesRegex(weatherbot.MeshError, "selecting outgoing flood scope.*unsupported"):
                await bot.send_channel(FakeMesh(commands), "weather")
            self.assertFalse(commands.channel_messages)
            self.assertEqual([call.args[0] for call in commands.set_flood_scope.call_args_list], ["#us-la-msy", None])
            self.assertFalse(bot._mesh_lock.locked())

    async def test_cleanup_after_send_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            commands = FakeSetupCommands()
            commands.send_chan_msg = AsyncMock(side_effect=RuntimeError("send failed"))
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                await bot.send_channel(FakeMesh(commands), "weather")
            self.assertEqual(commands.scope_calls, ["#us-la-msy", None])
            self.assertIsNone(commands.scope)
            self.assertFalse(bot._mesh_lock.locked())

    async def test_cleanup_failure_preserves_original_error(self):
        with tempfile.TemporaryDirectory() as directory:
            for send_fails in (False, True):
                with self.subTest(send_fails=send_fails):
                    bot = self.make_bot(directory)
                    commands = FakeSetupCommands()
                    commands.set_flood_scope = AsyncMock(side_effect=[
                        FakeEvent(EventType.OK),
                        FakeEvent(EventType.ERROR, {"reason": "cleanup failed"}),
                    ])
                    if send_fails:
                        commands.send_chan_msg = AsyncMock(side_effect=RuntimeError("send failed"))
                    expected = RuntimeError if send_fails else weatherbot.MeshError
                    reason = "send failed" if send_fails else "cleanup failed"
                    with self.assertLogs("weatherbot", level="ERROR") as logs:
                        with self.assertRaisesRegex(expected, reason):
                            await bot.send_channel(FakeMesh(commands), "weather")
                    self.assertIn("Could not clear outgoing flood scope override", " ".join(logs.output))
                    self.assertFalse(bot._mesh_lock.locked())

    async def test_concurrent_sends_do_not_change_active_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            commands = FakeSetupCommands()
            mesh = FakeMesh(commands)
            started = asyncio.Event()
            release = asyncio.Event()
            send = commands.send_chan_msg

            async def blocked_send(index, text):
                if text == "weather":
                    started.set()
                    await release.wait()
                return await send(index, text)

            commands.send_chan_msg = blocked_send
            regional = asyncio.create_task(bot.send_channel(mesh, "weather"))
            await asyncio.wait_for(started.wait(), 1)
            api = asyncio.create_task(bot.send_channel(mesh, "api", 3, api=True))
            try:
                await asyncio.sleep(0)
                self.assertEqual(commands.scope_calls, ["#us-la-msy"])
                release.set()
                await asyncio.wait_for(asyncio.gather(regional, api), 1)
            finally:
                release.set()
                await asyncio.gather(regional, api, return_exceptions=True)
            self.assertEqual(commands.sent_scopes, ["#us-la-msy", "*"])
            self.assertEqual(commands.scope_calls, ["#us-la-msy", None, "*", None])

    async def test_cancelled_send_clears_scope_before_unlocking(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            commands = FakeSetupCommands()
            started = asyncio.Event()

            async def blocked_send(index, text):
                started.set()
                await asyncio.Event().wait()

            commands.send_chan_msg = blocked_send
            task = asyncio.create_task(bot.send_channel(FakeMesh(commands), "weather"))
            try:
                await asyncio.wait_for(started.wait(), 1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(commands.scope_calls, ["#us-la-msy", None])
            self.assertIsNone(commands.scope)
            self.assertFalse(bot._mesh_lock.locked())

    async def test_setup_rejects_unsupported_scope_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            for api in (False, True):
                with self.subTest(api=api):
                    bot = self.make_bot(directory)
                    commands = FakeSetupCommands()
                    events = [FakeEvent(EventType.OK)] * (2 if api else 0)
                    events += [FakeEvent(EventType.ERROR, {"reason": "unsupported"}), FakeEvent(EventType.OK)]
                    commands.set_flood_scope = AsyncMock(side_effect=events)
                    with self.assertRaisesRegex(weatherbot.MeshError, "selecting outgoing flood scope.*unsupported"):
                        await bot._prepare_mesh(FakeMesh(commands))
                    self.assertNotIn(("send_advert", True), commands.calls)
                    self.assertFalse(bot._mesh_lock.locked())


class FakeBriefWeather:
    async def weather_report(self, zip_code):
        return f"WX {zip_code}: Clear, 72F."

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


class RegionTests(unittest.IsolatedAsyncioTestCase):
    FRAME = "14cfd0cfd00001020300000000000000000000000000000000"

    def test_firmware_transport_vectors_and_reserved_codes(self):
        # SHA256('#us-la-msy')[:16], HMAC(type || encrypted payload)[:2].
        # The latter two vectors produce 0000/ffff before firmware adjustment.
        for frame in (
            self.FRAME,
            "140100010000010203f7250100000000000000000000000000",
            "14fefffeff00010203181d0000000000000000000000000000",
        ):
            with self.subTest(frame=frame):
                self.assertTrue(weatherbot.raw_region_match({"payload": frame}))
        self.assertFalse(weatherbot.raw_region_match({"payload": "140000" + self.FRAME[6:]}))
        # The second transport code cannot substitute for the first.
        self.assertFalse(weatherbot.raw_region_match({"payload": "14abcd" + self.FRAME[6:]}))
        self.assertFalse(weatherbot.raw_region_match({"payload": "1500" + self.FRAME[12:]}))
        self.assertTrue(weatherbot.raw_region_match({"payload": "17" + self.FRAME[2:]}))
        for path_byte, path in (("01", "ab"), ("41", "abcd"), ("81", "abcdef")):
            self.assertTrue(weatherbot.raw_region_match({
                "payload": self.FRAME[:10] + path_byte + path + self.FRAME[12:]
            }))
        for value in (None, "zz", "", "14", "1400000000", "15ff", "54" + self.FRAME[2:]):
            self.assertIsNone(weatherbot.raw_region_match({"payload": value}))

    async def test_raw_log_correlation_and_expiration(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(make_config(Path(directory) / "state.json"), weather=FakeBriefWeather())
            event = dict(chan_name="Weather", sender_timestamp=100,
                         message="Alice: wx 60601", path="", path_hash_size=1,
                         payload=self.FRAME, route_typename="TC_FLOOD")
            message = weatherbot.InboundMessage("Alice: wx 60601", channel_index=1, sender_timestamp=100)
            with patch.object(weatherbot.time, "monotonic", return_value=100):
                bot._remember_raw_channel_event(event)
                matched = bot._attach_raw_channel_path(message)
                self.assertTrue(matched.required_region_match)
                self.assertEqual(matched.region, "us-la-msy")
                self.assertEqual(matched.path, "")
                for changes in (dict(text="Bob: wx 60601"), dict(channel_index=2),
                                dict(channel_index=None), dict(sender_timestamp=101)):
                    self.assertIsNone(bot._attach_raw_channel_path(weatherbot.replace(message, **changes)).required_region_match)
            with patch.object(weatherbot.time, "monotonic", return_value=111):
                self.assertIsNone(bot._attach_raw_channel_path(message).required_region_match)
                self.assertFalse(bot._raw_channel_paths)
            bot._remember_raw_channel_event(event)
            bot._remember_raw_channel_event({**event, "payload": "140000" + self.FRAME[6:]})
            conflicting = bot._attach_raw_channel_path(weatherbot.replace(message, region="us-la-msy"))
            self.assertIsNone(conflicting.required_region_match)
            self.assertFalse(weatherbot.has_required_region(conflicting))
            for timestamp in range(weatherbot.RAW_PATH_CACHE_LIMIT + 10):
                bot._remember_raw_channel_event({**event, "sender_timestamp": timestamp})
            self.assertEqual(len(bot._raw_channel_paths), weatherbot.RAW_PATH_CACHE_LIMIT)

    async def test_connection_clears_raw_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(make_config(Path(directory) / "state.json"), weather=FakeBriefWeather())
            bot._remember_raw_channel_path(100, "Alice: ping", "", 0, channel_index=2)
            class StopOnSubscribe:
                def subscribe(self, *args, **kwargs):
                    raise RuntimeError("stop before network")
            with self.assertRaisesRegex(RuntimeError, "stop before network"):
                await bot._serve_connection(StopOnSubscribe())
            self.assertFalse(bot._raw_channel_paths)

    async def test_channel_commands_require_region(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(make_config(Path(directory) / "state.json"), weather=FakeBriefWeather())
            bot.weather.weather_report = AsyncMock(return_value="WX 60601: Clear, 72F.")
            bot.weather.weather_json = AsyncMock(return_value={"type": "current", "t": 72})
            for command, channel, answer in (
                ("wx 60601", 1, "WX 60601: Clear, 72F."),
                ("wx help", 1, "Gulf Coast Mesh Bot"),
                ("wx version", 1, "Gulf Coast Mesh Bot version:"),
                ("wx report 60601", 1, "Please run wx report"),
                ("wx report stop", 1, "Please run wx report"),
                ("ping", 2, "Pong"),
                ("wx 60601 json", 1, '"current"'),
                ("wx help json", 1, '"help"'),
                ("wx version json", 1, '"version"'),
                ("wx report 60601 json", 1, '"error"'),
                ("wx report stop json", 1, '"error"'),
                ("ping json", 2, '"pong"'),
            ):
                for status in (None, False, True):
                    with self.subTest(command=command, status=status):
                        commands = FakeSetupCommands()
                        bot.weather.weather_report.reset_mock()
                        bot.weather.weather_json.reset_mock()
                        bot._seen_requests.clear()
                        message = weatherbot.InboundMessage(
                            "Alice: " + command, channel_index=channel,
                            sender_timestamp=100, required_region_match=status,
                        )
                        handled = await bot.handle_message(FakeMesh(commands), message)
                        self.assertEqual(handled, status is True)
                        if status is not True:
                            self.assertFalse(commands.channel_messages)
                            self.assertFalse(commands.calls)
                            bot.weather.weather_report.assert_not_awaited()
                            bot.weather.weather_json.assert_not_awaited()
                            self.assertFalse(bot._seen_requests)
                            continue
                        chunks = [text for _, text in commands.channel_messages]
                        body = " ".join(weatherbot.re.sub(r"^\[\d+/\d+\] ", "", text) for text in chunks)
                        self.assertIn(answer, body)
                        self.assertNotIn('Missing "us-la-msy" region.', body)
                        self.assertEqual(commands.sent_scopes, ["#us-la-msy"] * len(chunks))
                        self.assertTrue(all(len(text.encode()) <= 140 for text in chunks))
            commands = FakeSetupCommands()
            self.assertFalse(await bot.handle_message(FakeMesh(commands), weatherbot.InboundMessage("Alice: hello", channel_index=1)))
            self.assertFalse(commands.channel_messages)
            bot.weather.weather_report = AsyncMock(side_effect=weatherbot.WeatherError("unavailable"))
            self.assertTrue(await bot.handle_message(FakeMesh(commands), weatherbot.InboundMessage("wx 60601", channel_index=1, required_region_match=True)))
            body = " ".join(text for _, text in commands.channel_messages)
            self.assertIn("lookup failed: unavailable", body)
            self.assertNotIn('Missing "us-la-msy" region.', body)

    async def test_api_and_dm_exemptions(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(make_config(Path(directory) / "state.json"), weather=FakeBriefWeather())
            bot.weather.weather_json = AsyncMock(return_value={"type": "current", "t": 72})
            for command in ("bot json api", "wx 60601 json all api", "bot api2",
                            "ping api2", "wx 60601 api2", "wx 60601 all api2"):
                for status in (None, False):
                    with self.subTest(command=command, status=status):
                        commands = FakeSetupCommands()
                        self.assertTrue(await bot.handle_message(FakeMesh(commands), weatherbot.InboundMessage(command, channel_index=3, required_region_match=status)))
                        self.assertTrue(commands.channel_messages)
                        self.assertEqual(commands.sent_scopes, ["*"] * len(commands.channel_messages))
                        self.assertNotIn('Missing "us-la-msy" region.', " ".join(text for _, text in commands.channel_messages))
            bot._has_contact = AsyncMock(return_value=True)
            bot.send_dm_with_fallback = AsyncMock()
            for command in ("wx help", "wx help json", "wx 60601", "wx 60601 json",
                            "wx report 60601", "wx report stop", "ping", "ping json",
                            "bot json api", "bot api2"):
                with self.subTest(dm_command=command):
                    bot.send_dm_with_fallback.reset_mock()
                    self.assertTrue(await bot.handle_message(FakeMesh(FakeRoutingCommands()), weatherbot.InboundMessage(command, sender_prefix="aabbccddeeff")))
                    bot.send_dm_with_fallback.assert_awaited()
            self.assertNotIn('Missing "us-la-msy" region.', " ".join(call.args[2] for call in bot.send_dm_with_fallback.call_args_list))

    async def test_region_fallback_controls_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(make_config(Path(directory) / "state.json"), weather=FakeBriefWeather())
            for region, status, accepted in (
                ("us-la-msy", None, True), ("#us-la-msy", None, True),
                (" us-la-msy ", None, True), ("us-gulf", None, False),
                ("US-LA-MSY", None, False), ("us-la-msy", False, False),
            ):
                with self.subTest(region=region, status=status):
                    commands = FakeSetupCommands()
                    self.assertEqual(await bot.handle_message(
                        FakeMesh(commands), weatherbot.InboundMessage(
                            "Alice: wx help", channel_index=1, region=region,
                            required_region_match=status,
                        ),
                    ), accepted)
                    self.assertEqual(bool(commands.channel_messages), accepted)

    def test_explicit_region_fallback_and_packet_precedence(self):
        for name in ("us-la-msy", "#us-la-msy", " us-la-msy "):
            self.assertTrue(weatherbot.has_required_region(weatherbot.InboundMessage("ping", region=name)))
        for name in (None, "us-msy", "us-gulf", "US-LA-MSY"):
            self.assertFalse(weatherbot.has_required_region(weatherbot.InboundMessage("ping", region=name)))
        self.assertFalse(weatherbot.has_required_region(weatherbot.InboundMessage("ping", region="us-la-msy", required_region_match=False)))


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
            bot._remember_raw_channel_path(100, "Alice: ping", "af2b8a10", 1, "FLOOD", channel_index=2)
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
        self.assertEqual(matched.message_type, "FLOOD")
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
                api_channel_key=base64.b64encode(key[::-1]).decode(),
            )
            bot = weatherbot.WeatherBot(config, weather=FakeBriefWeather())
            commands = FakeSetupCommands()
            mesh = FakeMesh(commands)
            await bot._prepare_mesh(mesh)
        self.assertIn(("set_channel", 1, "Weather", key), commands.calls)
        self.assertIn(("set_channel", 3, "wx-bot-hidden", key[::-1]), commands.calls)
        self.assertIn(("set_path_hash_mode", 1), commands.calls)
        self.assertIn(("get_path_hash_mode",), commands.calls)
        self.assertIn(("send_advert", True), commands.calls)
        self.assertLess(
            commands.calls.index(("get_path_hash_mode",)),
            commands.calls.index(("send_advert", True)),
        )
        self.assertIn("aabbccddeeff", bot._contacts)
        self.assertTrue(mesh.decrypt_channel_logs)
        self.assertEqual(commands.scope_calls, ["#us-la-msy", None, "*", None])

    async def test_setup_fails_when_two_byte_path_hash_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(
                make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
            )
            commands = FakeSetupCommands(
                path_hash_set_event=FakeEvent(
                    EventType.ERROR, {"reason": "unsupported"}
                )
            )
            with self.assertRaisesRegex(
                weatherbot.MeshError, "setting two-byte path hashes failed: unsupported"
            ):
                await bot._prepare_mesh(FakeMesh(commands))

        self.assertNotIn(("send_advert", True), commands.calls)

    async def test_setup_fails_when_two_byte_path_hash_mode_does_not_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(
                make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
            )
            commands = FakeSetupCommands(path_hash_mode=0)
            with self.assertRaisesRegex(
                weatherbot.MeshError, "companion reported mode 0, expected 1"
            ):
                await bot._prepare_mesh(FakeMesh(commands))

        self.assertNotIn(("send_advert", True), commands.calls)

    async def test_channel_request_replies_to_weather_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(
                make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
            )
            commands = FakeSetupCommands()
            handled = await bot.handle_message(
                FakeMesh(commands),
                weatherbot.InboundMessage("Alice: wx 60601", channel_index=1, required_region_match=True),
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
                    mesh, weatherbot.InboundMessage("wx 60601 json all api", channel_index=3)
                )
            )
        self.assertEqual(len(commands.channel_messages), 3)
        envelopes = [json.loads(text) for _index, text in commands.channel_messages]
        self.assertTrue(all(envelope["d"].get("k") != "wx" for envelope in envelopes))
        self.assertTrue(all(len(text.encode("utf-8")) <= 140 for _index, text in commands.channel_messages))
        self.assertTrue(all(index == 3 for index, _text in commands.channel_messages))


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
            self.assertEqual(commands.sent_scopes, ["#us-la-msy"] * first_count)
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
            self.assertEqual(commands.sent_scopes, ["#us-la-msy"] * len(commands.attempts))
            sent = len(commands.attempts)
            self.assertEqual(await bot.poll_alerts(mesh), 0)
            self.assertEqual(len(commands.attempts), sent)

    async def test_ping_and_json_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = weatherbot.WeatherBot(
                make_config(Path(directory) / "state.json"), weather=FakeBriefWeather()
            )
            bot._contacts["aabbccddeeff"] = {
                "public_key": "aabbccddeeff" + "00" * 26,
                "adv_name": "Scarlett",
            }
            named_message = await bot._with_ping_sender_name(
                FakeMesh(FakeRoutingCommands()),
                weatherbot.InboundMessage("ping", sender_prefix="aabbccddeeff"),
            )
        self.assertEqual(named_message.sender_name, "Scarlett")
        self.assertEqual(weatherbot.requester_mention(named_message), "@[Scarlett]")
        message = weatherbot.InboundMessage(
            "Alice: ping",
            channel_index=2,
            path="aabbccdd",
            path_hash_mode=0,
            received_at=weatherbot.datetime(2026, 8, 24, 12, 0, tzinfo=weatherbot.ZoneInfo("UTC")),
        )
        self.assertEqual(
            weatherbot.format_ping_response(message),
            "@[Alice] 🏓 Pong\n"
            "Received: 12:00:00.000 UTC\n"
            "Path: AA-BB-CC-DD\n"
            "Hops: 4",
        )
        wide_path = weatherbot.InboundMessage("ping", path="af2b8a10", path_hash_mode=1)
        self.assertEqual(weatherbot.route_description(wide_path), "AF2B-8A10")
        self.assertEqual(weatherbot.hop_count(wide_path), 2)
        self.assertEqual(
            weatherbot.format_ping_response(
                weatherbot.InboundMessage(
                    "ping",
                    sender_prefix="aabbccddeeff",
                    sender_name="Alice",
                    path_len=3,
                    region="us-gulf",
                    message_type="TC_FLOOD",
                    received_at=weatherbot.datetime(
                        2026, 8, 24, 12, 0, 5, 525000,
                        tzinfo=weatherbot.ZoneInfo("UTC"),
                    ),
                )
            ),
            "@[Alice] 🏓 Pong\n"
            "Received: 12:00:05.525 UTC\n"
            "Path: 3 hops\n"
            "Region: us-gulf\n"
            "Hops: 3\n"
            "Message Type: TC Flood",
        )
        self.assertEqual(
            weatherbot.requester_mention(
                weatherbot.InboundMessage("ping", sender_prefix="aabbccddeeff")
            ),
            "@[aabbccddeeff]",
        )
        self.assertEqual(weatherbot.region_or_none({"region_name": " us-gulf "}), "us-gulf")
        self.assertIsNone(weatherbot.region_or_none({"transport_code": "deadbeef"}))
        self.assertEqual(weatherbot.route_type_or_none({"route_typename": "direct"}), "DIRECT")
        self.assertIsNone(weatherbot.route_type_or_none({"route_typename": "unknown"}))
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
