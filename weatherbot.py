#!/usr/bin/env python3
"""NOAA weather bot for an openHop Repeater companion TCP port."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

import httpx
from meshcore import EventType, MeshCore


LOG = logging.getLogger("weatherbot")
WX_COMMAND = re.compile(r"\s*wx\s+(\d{5})(?:-\d{4})?(?:\s+(json))?\s*", re.IGNORECASE)
WX_HELP_COMMAND = re.compile(r"\s*wx\s+help(?:\s+(json))?\s*", re.IGNORECASE)
WX_VERSION_COMMAND = re.compile(r"\s*wx\s+version(?:\s+(json))?\s*", re.IGNORECASE)
WX_REPORT_COMMAND = re.compile(
    r"\s*wx\s+report\s+(stop|(\d{5})(?:-\d{4})?)(?:\s+(json))?\s*", re.IGNORECASE
)
PING_COMMAND = re.compile(r"\s*ping(?:\s+(json))?\s*", re.IGNORECASE)
RAW_PATH_CACHE_SECONDS = 10.0
RAW_PATH_CACHE_LIMIT = 128
HELP_TEXT = (
    "Gulf Coast Mesh Bot, Designed by ScarlettOSA\n"
    "wx ZIPCODE: weather report\n"
    "wx report ZIPCODE: DM alert signup\n"
    "wx report stop: stop DM alerts\n"
    "wx version: running Git commit\n"
    "ping: DM or #test"
)
REPORT_STOP_TEXT = "To stop these alerts: wx report stop"
UNKNOWN_SENDER_NOTICE = (
    "⚠️ This reply was flood-sent because I do not have your advert. "
    "Please send an advert for reliable future replies."
)


class MeshError(RuntimeError):
    """A companion command or routing operation failed."""


class WeatherError(RuntimeError):
    """A ZIP or NWS lookup failed in a way safe to report to the requester."""


@dataclass(frozen=True)
class InboundMessage:
    text: str
    sender_prefix: Optional[str] = None
    channel_index: Optional[int] = None
    path: Optional[str] = None
    path_len: Optional[int] = None
    path_hash_mode: Optional[int] = None
    sender_timestamp: Optional[int] = None
    approx_direct_miles: Optional[float] = None
    received_at: Optional[datetime] = None

    @property
    def is_channel(self) -> bool:
        return self.channel_index is not None


@dataclass(frozen=True)
class Location:
    zip_code: str
    latitude: float
    longitude: float
    city: str
    state: str
    station_url: Optional[str]
    hourly_url: Optional[str]


@dataclass(frozen=True)
class BotConfig:
    repeater_host: str
    repeater_port: int
    bot_name: str
    weather_channel_index: int
    weather_channel_name: str
    weather_channel_key: str
    test_channel_index: int
    test_channel_name: str
    alert_zip_codes: list[str]
    alert_poll_seconds: int
    message_poll_seconds: float
    direct_retries: int
    ack_timeout_seconds: float
    reconnect_seconds: float
    request_dedup_seconds: float
    http_timeout_seconds: float
    noaa_user_agent: str
    state_file: Path
    log_level: str


class WeatherService:
    """Resolve ZIPs and retrieve observations, forecasts, and alerts."""

    def __init__(self, user_agent: str, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self.locations: dict[str, Location] = {}
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/geo+json, application/json",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _get_json(
        self, url: str, params: Optional[dict[str, str]] = None
    ) -> dict[str, Any]:
        try:
            response = await self.client.get(url, params=params)
            if response.status_code == 404:
                raise WeatherError("ZIP code was not found")
            response.raise_for_status()
            data = response.json()
        except WeatherError:
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            raise WeatherError(f"weather service returned HTTP {status}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise WeatherError("weather service is unavailable") from exc
        if not isinstance(data, dict):
            raise WeatherError("weather service returned invalid data")
        return data

    async def resolve_zip(self, zip_code: str) -> Location:
        zip_code = normalize_zip(zip_code)
        if zip_code in self.locations:
            return self.locations[zip_code]

        geo = await self._get_json(f"https://api.zippopotam.us/us/{zip_code}")
        places = geo.get("places") or []
        if not places:
            raise WeatherError("ZIP code was not found")
        place = places[0]
        try:
            latitude = float(place["latitude"])
            longitude = float(place["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherError("ZIP service returned invalid coordinates") from exc

        point = await self._get_json(
            f"https://api.weather.gov/points/{latitude:.4f},{longitude:.4f}"
        )
        properties = point.get("properties") or {}
        relative = ((properties.get("relativeLocation") or {}).get("properties") or {})
        city = str(relative.get("city") or place.get("place name") or zip_code)
        state = str(relative.get("state") or place.get("state abbreviation") or "")
        hourly_url = properties.get("forecastHourly")
        stations_url = properties.get("observationStations")
        station_url: Optional[str] = None
        if stations_url:
            try:
                stations = await self._get_json(str(stations_url))
                features = stations.get("features") or []
                if features:
                    station_url = features[0].get("id") or (
                        features[0].get("properties") or {}
                    ).get("@id")
            except WeatherError:
                LOG.warning("Could not resolve observation station for %s", zip_code)

        location = Location(
            zip_code=zip_code,
            latitude=latitude,
            longitude=longitude,
            city=city,
            state=state,
            station_url=str(station_url) if station_url else None,
            hourly_url=str(hourly_url) if hourly_url else None,
        )
        self.locations[zip_code] = location
        return location

    async def active_alerts(self, location: Location) -> list[dict[str, Any]]:
        data = await self._get_json(
            "https://api.weather.gov/alerts/active",
            params={"point": f"{location.latitude:.4f},{location.longitude:.4f}"},
        )
        return [item for item in (data.get("features") or []) if isinstance(item, dict)]

    async def weather_report(self, zip_code: str) -> str:
        location = await self.resolve_zip(zip_code)
        condition_lines = await self._current_conditions(location)
        alerts = await self.active_alerts(location)
        lines = [f"☀️ {location.city}, {location.state} {location.zip_code}"]
        lines.extend(condition_lines)
        if not alerts:
            lines.append("✅ No active NWS alerts")
        else:
            for alert in alerts:
                properties = alert.get("properties") or {}
                event = clean_text(str(properties.get("event") or "Weather Alert"))
                severity = clean_text(str(properties.get("severity") or "Unknown"))
                ends = format_alert_time(
                    properties.get("ends") or properties.get("expires"),
                    str(properties.get("timeZone") or ""),
                )
                timing = f", until {ends}" if ends else ""
                lines.append(f"⚠️ {event} ({severity}{timing})")
        return "\n".join(lines)

    async def weather_json(self, zip_code: str) -> dict[str, Any]:
        """Return the small weather summary shown in the text report."""
        location = await self.resolve_zip(zip_code)
        conditions: dict[str, Any] = {}
        source = ""
        if location.station_url:
            try:
                observation = await self._get_json(
                    location.station_url.rstrip("/") + "/observations/latest"
                )
                conditions = observation.get("properties") or {}
                source = "observation"
            except WeatherError:
                pass
        if not conditions and location.hourly_url:
            try:
                hourly = await self._get_json(location.hourly_url)
                periods = (hourly.get("properties") or {}).get("periods") or []
                if periods and isinstance(periods[0], dict):
                    conditions = periods[0]
                    source = "hourly_forecast"
            except WeatherError:
                pass
        alerts = await self.active_alerts(location)
        report: dict[str, Any] = {
            "z": location.zip_code,
            "l": f"{location.city}, {location.state}",
        }

        if source == "observation":
            temperature = quantity(conditions.get("temperature"))
            if temperature is not None:
                report["t"] = round(
                    to_fahrenheit(temperature, unit_code(conditions.get("temperature")))
                )
            description = clean_text(str(conditions.get("textDescription") or ""))
            if description:
                report["c"] = description
            humidity = quantity(conditions.get("relativeHumidity"))
            if humidity is not None:
                report["h"] = round(humidity)
            wind = quantity(conditions.get("windSpeed"))
            if wind is not None:
                mph = to_mph(wind, unit_code(conditions.get("windSpeed")))
                if mph < 1:
                    report["w"] = "calm"
                else:
                    direction = quantity(conditions.get("windDirection"))
                    compass = degrees_to_compass(direction) if direction is not None else ""
                    report["w"] = f"{compass} {round(mph)} mph".strip()
        elif source == "hourly_forecast":
            temperature = conditions.get("temperature")
            if temperature is not None:
                value = float(temperature)
                if str(conditions.get("temperatureUnit") or "F").upper() == "C":
                    value = value * 9 / 5 + 32
                report["t"] = round(value)
            forecast = clean_text(str(conditions.get("shortForecast") or ""))
            if forecast:
                report["c"] = forecast
            wind = clean_text(
                f"{conditions.get('windDirection') or ''} {conditions.get('windSpeed') or ''}"
            )
            if wind:
                report["w"] = wind

        alert_summaries: list[list[str]] = []
        for alert in alerts:
            properties = alert.get("properties") or {}
            event = clean_text(str(properties.get("event") or "Weather Alert"))
            severity = clean_text(str(properties.get("severity") or "Unknown"))
            summary = [event, severity]
            ends = format_alert_time(
                properties.get("ends") or properties.get("expires"),
                str(properties.get("timeZone") or ""),
            )
            if ends:
                summary.append(ends)
            alert_summaries.append(summary)
        report["a"] = alert_summaries
        return report

    async def _current_conditions(self, location: Location) -> list[str]:
        if location.station_url:
            try:
                observation = await self._get_json(
                    location.station_url.rstrip("/") + "/observations/latest"
                )
                properties = observation.get("properties") or {}
                temperature = quantity(properties.get("temperature"))
                description = clean_text(str(properties.get("textDescription") or ""))
                lines: list[str] = []
                temperature_line = ""
                if temperature is not None:
                    fahrenheit = to_fahrenheit(
                        temperature, unit_code(properties.get("temperature"))
                    )
                    temperature_line = f"{round(fahrenheit)}°F"
                if description:
                    temperature_line = (
                        f"{temperature_line} · {description}"
                        if temperature_line
                        else description
                    )
                if temperature_line:
                    lines.append(f"🌡️ {temperature_line}")

                humidity = quantity(properties.get("relativeHumidity"))
                wind_parts: list[str] = []
                wind = quantity(properties.get("windSpeed"))
                if wind is not None:
                    mph = to_mph(wind, unit_code(properties.get("windSpeed")))
                    direction = quantity(properties.get("windDirection"))
                    if mph < 1:
                        wind_parts.append("calm")
                    else:
                        compass = (
                            degrees_to_compass(direction)
                            if direction is not None
                            else ""
                        )
                        wind_parts.append(f"{compass} {round(mph)} mph")
                stats: list[str] = []
                if humidity is not None:
                    stats.append(f"💧 {round(humidity)}%")
                if wind_parts:
                    stats.append(f"💨 {wind_parts[0]}")
                if stats:
                    lines.append(" · ".join(stats))
                if lines:
                    return lines
            except WeatherError:
                LOG.warning(
                    "Latest observation unavailable for %s; using hourly forecast",
                    location.zip_code,
                )

        if location.hourly_url:
            hourly = await self._get_json(location.hourly_url)
            periods = (hourly.get("properties") or {}).get("periods") or []
            if periods:
                period = periods[0]
                temperature = period.get("temperature")
                if temperature is not None and str(
                    period.get("temperatureUnit") or "F"
                ).upper() == "C":
                    temperature = float(temperature) * 9 / 5 + 32
                lines: list[str] = []
                temperature_line = ""
                if temperature is not None:
                    temperature_line = f"{round(float(temperature))}°F"
                if period.get("shortForecast"):
                    forecast = clean_text(str(period["shortForecast"]))
                    temperature_line = (
                        f"{temperature_line} · {forecast}"
                        if temperature_line
                        else forecast
                    )
                if temperature_line:
                    lines.append(f"🌡️ {temperature_line}")
                if period.get("windSpeed"):
                    wind_direction = period.get("windDirection") or ""
                    lines.append(f"💨 {wind_direction} {period['windSpeed']}".strip())
                if lines:
                    lines.append("(current-hour NWS forecast)")
                    return lines
        raise WeatherError("current conditions are unavailable")


class WeatherBot:
    def __init__(self, config: BotConfig, weather: Optional[WeatherService] = None) -> None:
        self.config = config
        self._owns_weather = weather is None
        self.weather = weather or WeatherService(
            config.noaa_user_agent, timeout=config.http_timeout_seconds
        )
        self._advertised = False
        self._contacts: dict[str, dict[str, Any]] = {}
        self._bot_coordinates: Optional[tuple[float, float]] = None
        self._raw_channel_paths: deque[tuple[float, int, str, str, int]] = deque(
            maxlen=RAW_PATH_CACHE_LIMIT
        )
        self._mesh_lock = asyncio.Lock()
        self._recent_acks: deque[str] = deque(maxlen=128)
        self._ack_signal = asyncio.Event()
        self._seen_requests: dict[tuple, float] = {}
        (
            self._seen_alerts,
            self._report_subscriptions,
            self._reported_alerts,
        ) = self._load_state()

    async def run_forever(self) -> None:
        try:
            while True:
                mesh = None
                try:
                    LOG.info(
                        "Connecting to openHop companion at %s:%d",
                        self.config.repeater_host,
                        self.config.repeater_port,
                    )
                    mesh = await MeshCore.create_tcp(
                        self.config.repeater_host,
                        self.config.repeater_port,
                    )
                    if mesh is None:
                        raise MeshError("companion did not complete MeshCore startup")
                    await self._serve_connection(mesh)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.error("Bot connection cycle failed: %s", exc)
                finally:
                    if mesh is not None:
                        try:
                            await mesh.disconnect()
                        except Exception as exc:
                            LOG.debug("Error while closing companion connection: %s", exc)

                LOG.info("Reconnecting in %.1f seconds", self.config.reconnect_seconds)
                await asyncio.sleep(self.config.reconnect_seconds)
        finally:
            if self._owns_weather:
                await self.weather.close()

    async def _serve_connection(self, mesh: Any) -> None:
        disconnected = asyncio.Event()

        def remember_ack(event: Any) -> None:
            code = str((event.payload or {}).get("code") or event.attributes.get("code") or "")
            if code:
                self._recent_acks.append(code.lower())
                self._ack_signal.set()

        def mark_disconnected(event: Any) -> None:
            payload = event.payload or {}
            reason = payload.get("reason") if isinstance(payload, dict) else payload
            LOG.warning("Companion connection disconnected: %s", reason or "unknown")
            disconnected.set()

        async def handle_dm(event: Any) -> None:
            payload = event.payload or {}
            message = InboundMessage(
                    text=str(payload.get("text") or ""),
                    sender_prefix=str(payload.get("pubkey_prefix") or "").lower(),
                    path=str(payload.get("path") or "") or None,
                    path_len=integer_or_none(payload.get("path_len")),
                    path_hash_mode=integer_or_none(payload.get("path_hash_mode")),
                    sender_timestamp=integer_or_none(payload.get("sender_timestamp")),
                    received_at=datetime.now(tz=ZoneInfo("UTC")),
                )
            await self._safe_handle_message(
                mesh,
                replace(message, approx_direct_miles=self._direct_distance_miles(message)),
            )

        async def handle_channel(event: Any) -> None:
            payload = event.payload or {}
            message = InboundMessage(
                    text=str(payload.get("text") or ""),
                    channel_index=int(payload.get("channel_idx", -1)),
                    path=str(payload.get("path") or "") or None,
                    path_len=integer_or_none(payload.get("path_len")),
                    path_hash_mode=integer_or_none(payload.get("path_hash_mode")),
                    sender_timestamp=integer_or_none(payload.get("sender_timestamp")),
                    received_at=datetime.now(tz=ZoneInfo("UTC")),
                )
            await self._safe_handle_message(mesh, self._attach_raw_channel_path(message))

        async def remember_raw_channel_path(event: Any) -> None:
            payload = event.payload or {}
            if str(payload.get("chan_name") or "").lstrip("#").casefold() != self.config.test_channel_name.casefold():
                return
            path = str(payload.get("path") or "")
            timestamp = integer_or_none(payload.get("sender_timestamp"))
            text = str(payload.get("message") or "")
            hash_size = integer_or_none(payload.get("path_hash_size"))
            if not path or timestamp is None or not text or hash_size not in (1, 2, 3):
                return
            self._remember_raw_channel_path(timestamp, text, path, hash_size - 1)

        async def drain_waiting(_event: Any) -> None:
            try:
                await self._drain_messages(mesh)
            except Exception as exc:
                LOG.error("Could not fetch queued mesh messages: %s", exc)

        subscriptions = [
            mesh.subscribe(EventType.ACK, remember_ack),
            mesh.subscribe(EventType.DISCONNECTED, mark_disconnected),
            mesh.subscribe(EventType.CONTACT_MSG_RECV, handle_dm),
            mesh.subscribe(EventType.RX_LOG_DATA, remember_raw_channel_path),
            mesh.subscribe(
                EventType.CHANNEL_MSG_RECV,
                handle_channel,
                attribute_filters={"channel_idx": self.config.weather_channel_index},
            ),
            mesh.subscribe(EventType.MESSAGES_WAITING, drain_waiting),
        ]
        if self.config.test_channel_index != self.config.weather_channel_index:
            subscriptions.append(
                mesh.subscribe(
                    EventType.CHANNEL_MSG_RECV,
                    handle_channel,
                    attribute_filters={"channel_idx": self.config.test_channel_index},
                )
            )
        alert_task: Optional[asyncio.Task[Any]] = None
        message_poll_task: Optional[asyncio.Task[Any]] = None
        try:
            await self._prepare_mesh(mesh)
            await self._drain_messages(mesh)
            message_poll_task = asyncio.create_task(
                self._message_poll_loop(mesh, disconnected),
                name="weatherbot-message-poll",
            )
            alert_task = asyncio.create_task(
                self._alert_loop(mesh), name="weatherbot-alerts"
            )
            await disconnected.wait()
        finally:
            for task in (message_poll_task, alert_task):
                if task is None:
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for subscription in subscriptions:
                mesh.unsubscribe(subscription)

    async def _prepare_mesh(self, mesh: Any) -> None:
        async with self._mesh_lock:
            self._bot_coordinates = coordinates_or_none(
                getattr(mesh, "self_info", {})
            )
            self._require_event(
                await mesh.commands.set_name(self.config.bot_name),
                EventType.OK,
                "setting the bot name",
            )
            if self.config.weather_channel_key:
                secret = decode_channel_key(self.config.weather_channel_key)
                self._require_event(
                    await mesh.commands.set_channel(
                        self.config.weather_channel_index,
                        self.config.weather_channel_name,
                        secret,
                    ),
                    EventType.OK,
                    "configuring #Weather",
                )

            channel = self._require_event(
                await mesh.commands.get_channel(self.config.weather_channel_index),
                EventType.CHANNEL_INFO,
                "reading #Weather",
            )
            actual = str(channel.payload.get("channel_name") or "").lstrip("#")
            expected = self.config.weather_channel_name.lstrip("#")
            if actual.casefold() != expected.casefold():
                raise MeshError(
                    f"channel {self.config.weather_channel_index} is "
                    f"{actual or 'unconfigured'!r}, not {expected!r}"
                )

            test_channel = self._require_event(
                await mesh.commands.get_channel(self.config.test_channel_index),
                EventType.CHANNEL_INFO,
                "reading #test",
            )
            actual_test = str(test_channel.payload.get("channel_name") or "").lstrip("#")
            expected_test = self.config.test_channel_name.lstrip("#")
            if actual_test.casefold() != expected_test.casefold():
                raise MeshError(
                    f"channel {self.config.test_channel_index} is "
                    f"{actual_test or 'unconfigured'!r}, not {expected_test!r}"
                )

            # MeshCore otherwise keeps RF logs encrypted and exposes only the
            # companion's hop count.  With the verified channel keys loaded,
            # it can correlate a raw #test packet with its delivered message.
            mesh.set_decrypt_channel_logs(True)

            await self._refresh_contacts_unlocked(mesh)
            if not self._advertised:
                self._require_event(
                    await mesh.commands.send_advert(flood=True),
                    EventType.OK,
                    "advertising the bot",
                )
                self._advertised = True
        LOG.info("Weather bot is ready on #%s", actual)

    def _remember_raw_channel_path(
        self, sender_timestamp: int, text: str, path: str, path_hash_mode: int
    ) -> None:
        now = time.monotonic()
        self._raw_channel_paths = deque(
            (item for item in self._raw_channel_paths if item[0] >= now - RAW_PATH_CACHE_SECONDS),
            maxlen=RAW_PATH_CACHE_LIMIT,
        )
        self._raw_channel_paths.append(
            (now, sender_timestamp, command_text(text, True), path, path_hash_mode)
        )

    def _attach_raw_channel_path(self, message: InboundMessage) -> InboundMessage:
        """Attach only an exact #test raw-log match; DMs are never guessed."""
        if (
            message.channel_index != self.config.test_channel_index
            or message.sender_timestamp is None
        ):
            return message
        now = time.monotonic()
        wanted_text = command_text(message.text, True)
        retained: deque[tuple[float, int, str, str, int]] = deque(
            maxlen=RAW_PATH_CACHE_LIMIT
        )
        match: Optional[tuple[str, int]] = None
        while self._raw_channel_paths:
            received, timestamp, text, path, mode = self._raw_channel_paths.popleft()
            if received < now - RAW_PATH_CACHE_SECONDS:
                continue
            if match is None and timestamp == message.sender_timestamp and text == wanted_text:
                match = (path, mode)
                continue
            retained.append((received, timestamp, text, path, mode))
        self._raw_channel_paths = retained
        return replace(message, path=match[0], path_hash_mode=match[1]) if match else message

    def _direct_distance_miles(self, message: InboundMessage) -> Optional[float]:
        if not message.sender_prefix or self._bot_coordinates is None:
            return None
        contact = self._contacts.get(message.sender_prefix[:12].lower())
        sender_coordinates = coordinates_or_none(contact or {})
        if sender_coordinates is None:
            return None
        return haversine_miles(self._bot_coordinates, sender_coordinates)

    async def _drain_messages(self, mesh: Any) -> None:
        async with self._mesh_lock:
            while mesh.connection_manager.is_connected:
                result = await mesh.commands.get_msg()
                if result.type == EventType.NO_MORE_MSGS:
                    return
                if result.type == EventType.ERROR:
                    raise MeshError(
                        "fetching queued messages failed: "
                        + str((result.payload or {}).get("reason") or result.payload)
                    )
                if result.type not in (
                    EventType.CONTACT_MSG_RECV,
                    EventType.CHANNEL_MSG_RECV,
                ):
                    raise MeshError(f"unexpected queued-message response: {result.type}")

    async def _message_poll_loop(self, mesh: Any, disconnected: asyncio.Event) -> None:
        """Periodically issue CMD_SYNC_NEXT_MESSAGE for companions without wake pushes."""
        while not disconnected.is_set():
            try:
                await asyncio.wait_for(
                    disconnected.wait(), timeout=self.config.message_poll_seconds
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._drain_messages(mesh)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Companion message sync failed: %s", exc)

    async def _safe_handle_message(self, mesh: Any, message: InboundMessage) -> None:
        try:
            command = command_text(message.text, message.is_channel)
            LOG.debug(
                "Received %s message%s (%d characters; command=%s)",
                "channel" if message.is_channel else "DM",
                (
                    f" on channel {message.channel_index}"
                    if message.is_channel
                    else f" from {message.sender_prefix[:12]}"
                    if message.sender_prefix
                    else ""
                ),
                len(message.text),
                "yes"
                if (
                    PING_COMMAND.fullmatch(command)
                    or WX_HELP_COMMAND.fullmatch(command)
                    or WX_VERSION_COMMAND.fullmatch(command)
                    or WX_REPORT_COMMAND.fullmatch(command)
                    or WX_COMMAND.fullmatch(command)
                )
                else "no",
            )
            await self.handle_message(mesh, message)
        except Exception as exc:
            LOG.error("Could not handle mesh message: %s", exc)

    def _request_key(self, message: InboundMessage) -> Optional[tuple]:
        """Return a stable identity for one received packet, if MeshCore supplied it."""
        if message.sender_timestamp is None:
            return None
        text_digest = hashlib.sha256(message.text.encode("utf-8")).hexdigest()
        if message.is_channel:
            if message.channel_index is None:
                return None
            return ("channel", message.channel_index, message.sender_timestamp, text_digest)
        if not message.sender_prefix:
            return None
        return ("dm", message.sender_prefix[:12], message.sender_timestamp, text_digest)

    def _note_seen(self, request_key: tuple) -> None:
        self._seen_requests = {
            key: expiry
            for key, expiry in self._seen_requests.items()
            if expiry > time.monotonic()
        }
        self._seen_requests[request_key] = time.monotonic() + self.config.request_dedup_seconds

    def _is_seen(self, request_key: tuple) -> bool:
        expiry = self._seen_requests.get(request_key)
        return expiry is not None and expiry > time.monotonic()

    async def handle_message(self, mesh: Any, message: InboundMessage) -> bool:
        if message.is_channel and message.channel_index not in (
            self.config.weather_channel_index,
            self.config.test_channel_index,
        ):
            return False
        command = command_text(message.text, message.is_channel)
        ping_match = PING_COMMAND.fullmatch(command)
        if ping_match:
            if message.is_channel and message.channel_index != self.config.test_channel_index:
                return False
            response: str | dict[str, Any] = (
                ping_response_data(message)
                if ping_match.group(1)
                else format_ping_response(message)
            )
            await self._reply(mesh, message, response)
            return True

        if message.is_channel and message.channel_index != self.config.weather_channel_index:
            return False
        help_match = WX_HELP_COMMAND.fullmatch(command)
        if help_match:
            response: str | dict[str, Any] = (
                help_response_data() if help_match.group(1) else HELP_TEXT
            )
            await self._reply(mesh, message, response)
            return True

        version_match = WX_VERSION_COMMAND.fullmatch(command)
        if version_match:
            version = git_commit()
            response = {"type": "version", "git_commit": version} if version_match.group(1) else f"Gulf Coast Mesh Bot version: {version}"
            await self._reply(mesh, message, response)
            return True

        report_match = WX_REPORT_COMMAND.fullmatch(command)
        if report_match:
            if message.is_channel:
                response: str | dict[str, Any] = (
                    {
                        "type": "error",
                        "command": "wx report",
                        "error": "run this command in a DM",
                    }
                    if report_match.group(3)
                    else "Please run wx report ZIPCODE or wx report stop in a DM."
                )
                await self._reply(mesh, message, response)
                return True
            if not message.sender_prefix:
                raise MeshError("a queued DM did not include its sender key prefix")
            requested = report_match.group(1).casefold()
            wants_json = bool(report_match.group(3))
            prefix = message.sender_prefix[:12].lower()
            if requested == "stop":
                removed = self._report_subscriptions.pop(prefix, [])
                self._reported_alerts.pop(prefix, None)
                self._save_state()
                reply: str | dict[str, Any] = (
                    {"type": "report", "status": "stopped", "zip_codes": removed}
                    if wants_json
                    else (
                        "WX reports stopped."
                        if removed
                        else "You do not have any active WX reports."
                    )
                )
            else:
                zip_code = normalize_zip(requested)
                subscriptions = self._report_subscriptions.setdefault(prefix, [])
                if zip_code not in subscriptions:
                    subscriptions.append(zip_code)
                    subscriptions.sort()
                    self._save_state()
                reply = (
                    {
                        "type": "report",
                        "status": "enabled",
                        "zip_code": zip_code,
                        "stop_command": "wx report stop",
                    }
                    if wants_json
                    else f"WX reports enabled for {zip_code}. {REPORT_STOP_TEXT}"
                )
            await self._reply(mesh, message, reply)
            return True

        request = parse_wx_request(command)
        if request is None:
            return False
        zip_code, wants_json = request

        request_key = self._request_key(message)
        if request_key is not None:
            if self._is_seen(request_key):
                LOG.info(
                    "Ignoring duplicate %s request for %s (retry or repeated packet)",
                    "channel" if message.is_channel else "DM",
                    zip_code,
                )
                return True
            self._note_seen(request_key)

        LOG.info(
            "Weather request for %s via %s",
            zip_code,
            "channel" if message.is_channel else "DM",
        )
        try:
            report: str | dict[str, Any] = (
                await self.weather.weather_json(zip_code)
                if wants_json
                else await self.weather.weather_report(zip_code)
            )
        except WeatherError as exc:
            report = (
                {"type": "error", "command": "wx", "zip_code": zip_code, "error": str(exc)}
                if wants_json
                else f"WX {zip_code}: lookup failed: {exc}."
            )

        await self._reply(mesh, message, report)
        return True

    async def _reply(
        self, mesh: Any, message: InboundMessage, response: str | dict[str, Any]
    ) -> None:
        if not message.is_channel:
            if not message.sender_prefix:
                raise MeshError("a queued DM did not include its sender key prefix")
            if not await self._has_contact(mesh, message.sender_prefix):
                response = (
                    {**response, "delivery_warning": UNKNOWN_SENDER_NOTICE}
                    if isinstance(response, dict)
                    else f"{response}\n\n{UNKNOWN_SENDER_NOTICE}"
                )
        chunks = (
            split_mesh_json(response)
            if isinstance(response, dict)
            else split_mesh_text(response)
        )
        if message.is_channel:
            for chunk in chunks:
                await self.send_channel(mesh, chunk, message.channel_index)
            return
        if not message.sender_prefix:
            raise MeshError("a queued DM did not include its sender key prefix")
        for chunk in chunks:
            LOG.debug("Sending DM chunk (%d bytes) to %s", len(chunk.encode("utf-8")), message.sender_prefix[:12])
            await self.send_dm_with_fallback(mesh, message.sender_prefix, chunk)

    async def send_channel(
        self, mesh: Any, text: str, channel_index: Optional[int] = None
    ) -> None:
        channel_index = self.config.weather_channel_index if channel_index is None else channel_index
        async with self._mesh_lock:
            self._require_event(
                await mesh.commands.send_chan_msg(channel_index, text),
                EventType.OK,
                "sending channel message",
            )

    async def send_dm_with_fallback(
        self, mesh: Any, sender_prefix: str | bytes, text: str
    ) -> bool:
        """Refresh the route from the newest advert path, then retry, then flood."""
        prefix = (
            sender_prefix.hex() if isinstance(sender_prefix, bytes) else sender_prefix
        ).lower()
        timestamp = int(time.time())

        async with self._mesh_lock:
            contact = await self._find_contact_unlocked(mesh, prefix)
            if contact is None:
                result = self._require_event(
                    await mesh.commands.send_msg(
                        prefix, text, timestamp=timestamp, attempt=0
                    ),
                    EventType.MSG_SENT,
                    "flooding a DM to an unknown sender",
                )
                if int(result.payload.get("type", 0)) != 1:
                    raise MeshError("companion did not flood the unknown-sender DM")
                LOG.warning("Flood-sent DM to unknown sender %s", prefix)
                return True
            await self._refresh_path_unlocked(mesh, contact, prefix)
            for attempt in range(self.config.direct_retries + 1):
                result = await mesh.commands.send_msg(
                    contact, text, timestamp=timestamp, attempt=attempt
                )
                if result.type == EventType.ERROR:
                    LOG.warning(
                        "Companion rejected DM to %s on attempt %d: %s",
                        prefix,
                        attempt + 1,
                        result.payload,
                    )
                    continue
                result = self._require_event(
                    result, EventType.MSG_SENT, "sending a routed DM"
                )
                if int(result.payload.get("type", 0)) == 1:
                    LOG.info("DM to %s used flood because no stored path exists", prefix)
                    return True

                expected_ack = result.payload.get("expected_ack", b"")
                ack_code = (
                    expected_ack.hex()
                    if isinstance(expected_ack, bytes)
                    else str(expected_ack)
                ).lower()
                suggested = max(
                    0.1, float(result.payload.get("suggested_timeout", 1000)) / 1000
                )
                timeout = min(suggested, self.config.ack_timeout_seconds)
                if await self._wait_for_ack(ack_code, timeout):
                    LOG.info("Routed DM to %s acknowledged", prefix)
                    return True
                LOG.warning(
                    "No ACK for routed DM to %s (attempt %d/%d)",
                    prefix,
                    attempt + 1,
                    self.config.direct_retries + 1,
                )

            self._require_event(
                await mesh.commands.reset_path(contact),
                EventType.OK,
                "resetting the failed contact path",
            )
            result = self._require_event(
                await mesh.commands.send_msg(
                    contact,
                    text,
                    timestamp=timestamp,
                    attempt=self.config.direct_retries + 1,
                ),
                EventType.MSG_SENT,
                "flooding a DM after routed retries",
            )
            if int(result.payload.get("type", 0)) != 1:
                raise MeshError("companion did not switch the failed route to flood")
            LOG.warning("Routed retries exhausted; flood-sent DM to %s", prefix)
            return True

    async def _refresh_path_unlocked(
        self, mesh: Any, contact: dict[str, Any], prefix: str
    ) -> None:
        """Refresh the contact's route from its newest advert path before sending."""
        try:
            result = await mesh.commands.get_advert_path(contact)
        except Exception as exc:
            LOG.warning("get_advert_path failed for %s: %s", prefix, exc)
            return
        if result is None or result.type == EventType.ERROR:
            LOG.warning(
                "get_advert_path returned %s for %s: %s",
                "no response" if result is None else "error",
                prefix,
                (result.payload or {}).get("reason") if result is not None else "",
            )
            return
        payload = result.payload or {}
        path = str(payload.get("path") or "")
        path_len = payload.get("path_len")
        path_hash_mode = payload.get("path_hash_mode")
        LOG.info(
            "Advert path for %s: len=%s mode=%s path=%s",
            prefix,
            path_len,
            path_hash_mode,
            path or "(none)",
        )
        if path_len is None or path_len < 0 or not path:
            LOG.info(
                "No routed advert path for %s; leaving the stored route unchanged",
                prefix,
            )
            return
        try:
            self._require_event(
                await mesh.commands.change_contact_path(contact, path, path_hash_mode),
                EventType.OK,
                "applying the advert path",
            )
            LOG.info("Applied advert path to %s (len=%s)", prefix, path_len)
        except Exception as exc:
            LOG.warning("Could not apply advert path to %s: %s", prefix, exc)

    async def _wait_for_ack(self, code: str, timeout: float) -> bool:
        if not code:
            return False
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                self._recent_acks.remove(code)
                return True
            except ValueError:
                pass
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            self._ack_signal.clear()
            try:
                self._recent_acks.remove(code)
                return True
            except ValueError:
                pass
            try:
                await asyncio.wait_for(self._ack_signal.wait(), remaining)
            except asyncio.TimeoutError:
                return False

    async def _has_contact(self, mesh: Any, prefix: str | bytes) -> bool:
        normalized = (prefix.hex() if isinstance(prefix, bytes) else prefix).lower()
        async with self._mesh_lock:
            return await self._find_contact_unlocked(mesh, normalized) is not None

    async def _find_contact_unlocked(
        self, mesh: Any, prefix: str
    ) -> Optional[dict[str, Any]]:
        contact = self._contacts.get(prefix[:12])
        if contact is None:
            await self._refresh_contacts_unlocked(mesh)
            contact = self._contacts.get(prefix[:12])
        return contact

    async def _refresh_contacts_unlocked(self, mesh: Any) -> None:
        result = self._require_event(
            await mesh.commands.get_contacts(),
            EventType.CONTACTS,
            "reading companion contacts",
        )
        contacts: dict[str, dict[str, Any]] = {}
        for contact in (result.payload or {}).values():
            if isinstance(contact, dict) and contact.get("public_key"):
                contacts[str(contact["public_key"]).lower()[:12]] = contact
        self._contacts = contacts

    @staticmethod
    def _require_event(result: Any, expected: EventType, action: str) -> Any:
        if result is None:
            raise MeshError(f"{action} failed: no response")
        if result.type == EventType.ERROR:
            payload = result.payload or {}
            reason = payload.get("reason") if isinstance(payload, dict) else payload
            raise MeshError(f"{action} failed: {reason or payload}")
        if result.type != expected:
            raise MeshError(f"{action} returned {result.type}, expected {expected}")
        return result

    async def _alert_loop(self, mesh: Any) -> None:
        while True:
            try:
                await self.poll_alerts(mesh)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.error("Scheduled alert check failed: %s", exc)
            await asyncio.sleep(self.config.alert_poll_seconds)

    async def poll_alerts(self, mesh: Any) -> int:
        monitored_zips = set(self.config.alert_zip_codes)
        for zip_codes in self._report_subscriptions.values():
            monitored_zips.update(zip_codes)
        if not monitored_zips:
            return 0
        grouped: dict[str, tuple[dict[str, Any], list[str]]] = {}
        for zip_code in sorted(monitored_zips):
            try:
                location = await self.weather.resolve_zip(zip_code)
                alerts = await self.weather.active_alerts(location)
            except WeatherError as exc:
                LOG.error("Alert check failed for %s: %s", zip_code, exc)
                continue
            for alert in alerts:
                key = alert_key(alert)
                if key in grouped:
                    grouped[key][1].append(zip_code)
                else:
                    grouped[key] = (alert, [zip_code])

        sent = 0
        for key, (alert, zip_codes) in grouped.items():
            channel_zips = sorted(set(zip_codes) & set(self.config.alert_zip_codes))
            if channel_zips and key not in self._seen_alerts:
                try:
                    for chunk in split_mesh_text(format_channel_alert(alert, channel_zips)):
                        await self.send_channel(mesh, chunk)
                except MeshError as exc:
                    LOG.error("Could not send NWS alert: %s", exc)
                else:
                    self._seen_alerts[key] = int(time.time())
                    self._save_state()
                    sent += 1
                    LOG.info("Sent new NWS alert for ZIP(s) %s", ", ".join(channel_zips))

            for prefix, subscriptions in self._report_subscriptions.items():
                matching_zips = sorted(set(zip_codes) & set(subscriptions))
                delivered = self._reported_alerts.get(prefix, {})
                if not matching_zips or key in delivered:
                    continue
                personal_alert = f"{format_channel_alert(alert, matching_zips)}\n{REPORT_STOP_TEXT}"
                try:
                    for chunk in split_mesh_text(personal_alert):
                        await self.send_dm_with_fallback(mesh, prefix, chunk)
                except MeshError as exc:
                    LOG.error("Could not send NWS report alert to %s: %s", prefix, exc)
                    continue
                self._reported_alerts.setdefault(prefix, {})[key] = int(time.time())
                self._save_state()
                sent += 1
                LOG.info("Sent NWS report alert to %s for ZIP(s) %s", prefix, ", ".join(matching_zips))
        return sent

    def _load_state(self) -> tuple[dict[str, int], dict[str, list[str]], dict[str, dict[str, int]]]:
        try:
            with self.config.state_file.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            seen = data.get("seen_alerts", {})
            seen_alerts = (
                {str(key): int(value) for key, value in seen.items()}
                if isinstance(seen, dict)
                else {}
            )
            subscriptions: dict[str, list[str]] = {}
            for prefix, values in (data.get("report_subscriptions") or {}).items():
                if not isinstance(values, list):
                    continue
                try:
                    subscriptions[str(prefix).lower()[:12]] = sorted(
                        set(normalize_zip(value) for value in values)
                    )
                except WeatherError:
                    continue
            reported: dict[str, dict[str, int]] = {}
            for prefix, values in (data.get("reported_alerts") or {}).items():
                if isinstance(values, dict):
                    reported[str(prefix).lower()[:12]] = {
                        str(key): int(value) for key, value in values.items()
                    }
            return seen_alerts, subscriptions, reported
        except FileNotFoundError:
            pass
        except (AttributeError, OSError, ValueError, TypeError) as exc:
            LOG.warning("Ignoring unreadable alert state: %s", exc)
        return {}, {}, {}

    def _save_state(self) -> None:
        self._seen_alerts = dict(list(self._seen_alerts.items())[-2000:])
        self._reported_alerts = {
            prefix: dict(list(alerts.items())[-2000:])
            for prefix, alerts in self._reported_alerts.items()
        }
        path = self.config.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "seen_alerts": self._seen_alerts,
                    "report_subscriptions": self._report_subscriptions,
                    "reported_alerts": self._reported_alerts,
                },
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
        os.replace(temporary, path)


def command_text(text: str, channel_message: bool = False) -> str:
    """Remove the sender label openHop prepends to channel text."""
    if channel_message and ": " in text:
        return text.split(": ", 1)[1]
    return text


def parse_wx_request(text: str) -> Optional[tuple[str, bool]]:
    match = WX_COMMAND.fullmatch(text)
    if match:
        return match.group(1), bool(match.group(2))
    return None


def parse_wx_command(text: str, channel_message: bool = False) -> Optional[str]:
    request = parse_wx_request(command_text(text, channel_message))
    return request[0] if request else None


def integer_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def coordinates_or_none(data: dict[str, Any]) -> Optional[tuple[float, float]]:
    """Return usable advertised coordinates, treating the default as unavailable."""
    try:
        latitude = float(data.get("adv_lat"))
        longitude = float(data.get("adv_lon"))
    except (TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    if latitude == 0 and longitude == 0:
        return None
    return latitude, longitude


def haversine_miles(
    first: tuple[float, float], second: tuple[float, float]
) -> float:
    first_latitude, first_longitude = map(math.radians, first)
    second_latitude, second_longitude = map(math.radians, second)
    latitude_delta = second_latitude - first_latitude
    longitude_delta = second_longitude - first_longitude
    value = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(first_latitude)
        * math.cos(second_latitude)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 3958.7613 * 2 * math.asin(math.sqrt(value))


def route_description(message: InboundMessage) -> str:
    if message.path:
        hash_size = (message.path_hash_mode or 0) + 1
        if hash_size > 0 and len(message.path) % (hash_size * 2) == 0:
            return "-".join(
                message.path[index : index + hash_size * 2].upper()
                for index in range(0, len(message.path), hash_size * 2)
            )
        return message.path.upper()
    if message.path_len is not None and message.path_len >= 0 and message.path_len != 255:
        return f"{message.path_len} hops"
    return "unavailable"


def ping_response_data(message: InboundMessage) -> dict[str, Any]:
    received = message.received_at or datetime.now(tz=ZoneInfo("UTC"))
    data: dict[str, Any] = {
        "type": "pong",
        "received_at": received.astimezone(ZoneInfo("UTC")).isoformat(),
        "path": route_description(message),
    }
    if message.approx_direct_miles is not None:
        data["approx_direct_miles"] = round(message.approx_direct_miles, 1)
    return data


def format_ping_response(message: InboundMessage) -> str:
    data = ping_response_data(message)
    response = f"🏓 Pong\nReceived: {data['received_at']}\nPath: {data['path']}"
    if "approx_direct_miles" in data:
        response += f"\nApprox. direct distance: {data['approx_direct_miles']:.1f} mi"
    return response


def help_response_data() -> dict[str, Any]:
    return {
        "type": "help",
        "service": "Gulf Coast Mesh Bot",
        "attribution": "Designed by ScarlettOSA",
        "commands": [
            "wx ZIPCODE",
            "wx report ZIPCODE",
            "wx report stop",
            "wx version",
            "ping",
        ],
        "json_modifier": "Append json to a command for a structured response.",
    }


def git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def normalize_zip(value: str) -> str:
    match = re.fullmatch(r"\s*(\d{5})(?:-\d{4})?\s*", str(value))
    if not match:
        raise WeatherError("expected a five-digit US ZIP code")
    return match.group(1)


def decode_channel_key(encoded: str) -> bytes:
    try:
        secret = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise MeshError("weather_channel_key is not valid Base64") from exc
    if len(secret) != 16:
        raise MeshError("weather_channel_key must decode to exactly 16 bytes")
    return secret


def clean_text(value: str) -> str:
    return " ".join(value.replace("\x00", " ").split())


def clean_lines(text: str) -> str:
    lines = [clean_text(line) for line in text.replace("\x00", " ").splitlines()]
    return "\n".join(line for line in lines if line)


def quantity(value: Any) -> Optional[float]:
    if not isinstance(value, dict) or value.get("value") is None:
        return None
    try:
        return float(value["value"])
    except (TypeError, ValueError):
        return None


def unit_code(value: Any) -> str:
    return str(value.get("unitCode") or "") if isinstance(value, dict) else ""


def to_fahrenheit(value: float, unit: str) -> float:
    return value * 9 / 5 + 32 if "degC" in unit else value


def to_mph(value: float, unit: str) -> float:
    if "km_h" in unit:
        return value * 0.621371
    if "m_s" in unit:
        return value * 2.23694
    if "knot" in unit:
        return value * 1.15078
    return value


def degrees_to_compass(degrees: float) -> str:
    points = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return points[int((degrees + 22.5) // 45) % 8]


def format_alert_time(value: Any, timezone: Optional[str] = None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timezone and parsed.tzinfo is not None:
            try:
                parsed = parsed.astimezone(ZoneInfo(timezone))
            except (KeyError, ValueError, TypeError):
                pass
        return parsed.strftime("%b %d %I:%M %p %Z").replace(" 0", " ").strip()
    except ValueError:
        return clean_text(str(value))[:40]


def alert_key(alert: dict[str, Any]) -> str:
    properties = alert.get("properties") or {}
    raw = "|".join(
        str(value or "")
        for value in (
            alert.get("id")
            or properties.get("id")
            or properties.get("@id")
            or properties.get("headline"),
            properties.get("sent") or properties.get("effective"),
            properties.get("messageType"),
        )
    )
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def format_channel_alert(alert: dict[str, Any], zip_codes: Sequence[str]) -> str:
    properties = alert.get("properties") or {}
    event = clean_text(str(properties.get("event") or "Weather Alert"))
    severity = clean_text(str(properties.get("severity") or "Unknown"))
    urgency = clean_text(str(properties.get("urgency") or "Unknown"))
    ends = format_alert_time(
        properties.get("ends") or properties.get("expires"),
        str(properties.get("timeZone") or ""),
    )
    description = clean_text(str(properties.get("description") or ""))
    headline = clean_text(str(properties.get("headline") or ""))
    instruction = clean_text(str(properties.get("instruction") or ""))
    timing = f", until {ends}" if ends else ""
    zips = ",".join(sorted(set(zip_codes)))
    lines = [f"🚨 NWS ALERT: {zips}", f"⚠️ {event} ({severity}, {urgency}{timing})"]
    body = description or headline or event
    if instruction and instruction.casefold() not in body.casefold():
        body = f"{body} {instruction}" if body else instruction
    if body:
        lines.append(body[:2000].rstrip())
    return "\n".join(lines)


def _cut_utf8(text: str, byte_limit: int) -> tuple[str, str]:
    if len(text.encode("utf-8")) <= byte_limit:
        return text, ""
    prefix = text.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore")
    split_at = max(prefix.rfind(" "), prefix.rfind("\n"))
    if split_at >= byte_limit // 3:
        prefix = prefix[:split_at]
    prefix = prefix.rstrip() or text[0]
    return prefix, text[len(prefix) :].lstrip()


def split_mesh_text(text: str, byte_limit: int = 140) -> list[str]:
    """Split text on line/word boundaries, leaving room for part numbers."""
    body_limit = byte_limit - 10
    lines = clean_lines(text).split("\n")
    chunks: list[str] = []
    current = ""
    for line in lines:
        if not line:
            continue
        while len(line.encode("utf-8")) > body_limit:
            piece, line = _cut_utf8(line, body_limit)
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece)
        candidate = f"{current}\n{line}" if current else line
        if len(candidate.encode("utf-8")) <= body_limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    if len(chunks) <= 1:
        return chunks
    total = len(chunks)
    return [f"[{index}/{total}] {chunk}" for index, chunk in enumerate(chunks, 1)]


def split_mesh_json(data: dict[str, Any], byte_limit: int = 140) -> list[str]:
    """Encode JSON compactly and split without adding non-JSON part markers.

    MeshCore messages are limited in size. Concatenating these chunks in order always
    reconstructs the original JSON document.
    """
    encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    chunks: list[str] = []
    remaining = encoded
    while remaining:
        piece, remaining = _cut_utf8(remaining, byte_limit)
        chunks.append(piece)
    return chunks or ["{}"]


def load_config(path: Path) -> BotConfig:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise SystemExit(
            f"Config file not found: {path} (copy config.json.example first)"
        ) from exc
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Could not read config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit("Config root must be a JSON object")

    state = Path(str(raw.get("state_file", ".weatherbot_state.json")))
    if not state.is_absolute():
        state = path.resolve().parent / state
    try:
        zip_codes = [normalize_zip(value) for value in raw.get("alert_zip_codes", [])]
    except WeatherError as exc:
        raise SystemExit(f"Invalid alert_zip_codes: {exc}") from exc

    config = BotConfig(
        repeater_host=str(raw.get("repeater_host", "127.0.0.1")),
        repeater_port=int(raw.get("repeater_port", 5001)),
        bot_name=str(raw.get("bot_name", "WeatherBot")),
        weather_channel_index=int(raw.get("weather_channel_index", 1)),
        weather_channel_name=str(raw.get("weather_channel_name", "Weather")).lstrip("#"),
        weather_channel_key=str(raw.get("weather_channel_key", "")),
        test_channel_index=int(raw.get("test_channel_index", 2)),
        test_channel_name=str(raw.get("test_channel_name", "test")).lstrip("#"),
        alert_zip_codes=list(dict.fromkeys(zip_codes)),
        alert_poll_seconds=int(raw.get("alert_poll_seconds", 60)),
        message_poll_seconds=float(raw.get("message_poll_seconds", 2)),
        direct_retries=int(raw.get("direct_retries", 3)),
        ack_timeout_seconds=float(raw.get("ack_timeout_seconds", 5)),
        reconnect_seconds=float(raw.get("reconnect_seconds", 5)),
        request_dedup_seconds=float(raw.get("request_dedup_seconds", 120)),
        http_timeout_seconds=float(raw.get("http_timeout_seconds", 15)),
        noaa_user_agent=str(raw.get("noaa_user_agent", "")).strip(),
        state_file=state,
        log_level=str(raw.get("log_level", "INFO")).upper(),
    )
    if not 1 <= config.repeater_port <= 65535:
        raise SystemExit("repeater_port must be between 1 and 65535")
    if not 0 <= config.weather_channel_index <= 39:
        raise SystemExit("weather_channel_index must be between 0 and 39")
    if not config.weather_channel_name:
        raise SystemExit("weather_channel_name cannot be empty")
    if not 0 <= config.test_channel_index <= 39:
        raise SystemExit("test_channel_index must be between 0 and 39")
    if not config.test_channel_name:
        raise SystemExit("test_channel_name cannot be empty")
    if config.test_channel_index == config.weather_channel_index:
        raise SystemExit("test_channel_index must differ from weather_channel_index")
    if not config.bot_name or len(config.bot_name.encode("utf-8")) > 31:
        raise SystemExit("bot_name must be 1-31 UTF-8 bytes")
    if config.alert_poll_seconds < 30:
        raise SystemExit("alert_poll_seconds must be at least 30")
    if config.message_poll_seconds <= 0:
        raise SystemExit("message_poll_seconds must be positive")
    if not 0 <= config.direct_retries <= 10:
        raise SystemExit("direct_retries must be between 0 and 10")
    if config.ack_timeout_seconds <= 0 or config.reconnect_seconds <= 0:
        raise SystemExit("ack_timeout_seconds and reconnect_seconds must be positive")
    if config.request_dedup_seconds <= 0:
        raise SystemExit("request_dedup_seconds must be positive")
    if config.http_timeout_seconds <= 0:
        raise SystemExit("http_timeout_seconds must be positive")
    if not config.noaa_user_agent:
        raise SystemExit("noaa_user_agent is required by api.weather.gov")
    if config.weather_channel_key:
        try:
            decode_channel_key(config.weather_channel_key)
        except MeshError as exc:
            raise SystemExit(str(exc)) from exc
    return config


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default="config.json", help="JSON config path")
    parser.add_argument(
        "--weather",
        metavar="ZIP",
        help="print one live weather report without connecting to the repeater",
    )
    return parser.parse_args(argv)


async def print_weather(config: BotConfig, zip_code: str) -> int:
    service = WeatherService(config.noaa_user_agent, config.http_timeout_seconds)
    try:
        print(await service.weather_report(zip_code))
        return 0
    except WeatherError as exc:
        LOG.error("Weather lookup failed: %s", exc)
        return 1
    finally:
        await service.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = load_config(Path(args.config))
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.weather:
        return asyncio.run(print_weather(config, args.weather))
    try:
        asyncio.run(WeatherBot(config).run_forever())
    except KeyboardInterrupt:
        LOG.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
