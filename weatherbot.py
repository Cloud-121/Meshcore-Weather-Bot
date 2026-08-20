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
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

import httpx
from meshcore import EventType, MeshCore


LOG = logging.getLogger("weatherbot")
WX_COMMAND = re.compile(r"\s*wx\s+(\d{5})(?:-\d{4})?\s*", re.IGNORECASE)
RX_PATH_WINDOW = 10.0
TEXT_MSG_PAYLOAD_TYPE = 2
DIRECT_ROUTE_TYPES = (2, 3)


class MeshError(RuntimeError):
    """A companion command or routing operation failed."""


class WeatherError(RuntimeError):
    """A ZIP or NWS lookup failed in a way safe to report to the requester."""


@dataclass(frozen=True)
class InboundMessage:
    text: str
    sender_prefix: Optional[str] = None
    channel_index: Optional[int] = None
    sender_timestamp: Optional[int] = None
    path_len: Optional[int] = None
    path_hash_mode: Optional[int] = None

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
    alert_zip_codes: list[str]
    alert_poll_seconds: int
    direct_retries: int
    ack_timeout_seconds: float
    reconnect_seconds: float
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
        self._mesh_lock = asyncio.Lock()
        self._recent_acks: deque[str] = deque(maxlen=128)
        self._ack_signal = asyncio.Event()
        self._seen_requests: deque[tuple] = deque(maxlen=256)
        self._recent_rx_paths: deque[tuple] = deque(maxlen=64)
        self._rx_paths: dict[str, tuple[str, int, float]] = {}
        self._seen_alerts = self._load_seen_alerts()

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

        def mark_disconnected(_event: Any) -> None:
            disconnected.set()

        def remember_rx_log(event: Any) -> None:
            payload = event.payload or {}
            if int(payload.get("payload_type") or -1) != TEXT_MSG_PAYLOAD_TYPE:
                return
            if int(payload.get("route_type") or -1) not in DIRECT_ROUTE_TYPES:
                return
            path = str(payload.get("path") or "")
            if not path:
                return
            self._recent_rx_paths.append(
                (
                    float(payload.get("recv_time") or 0),
                    int(payload.get("path_len") or 0),
                    int(payload.get("path_hash_size") or 1),
                    path,
                )
            )

        async def handle_dm(event: Any) -> None:
            payload = event.payload or {}
            message = InboundMessage(
                text=str(payload.get("text") or ""),
                sender_prefix=str(payload.get("pubkey_prefix") or "").lower(),
                sender_timestamp=int(payload["sender_timestamp"])
                if payload.get("sender_timestamp") is not None
                else None,
                path_len=_path_len(payload),
                path_hash_mode=_path_hash_mode(payload),
            )
            self._associate_rx_path(message)
            await self._safe_handle_message(mesh, message)

        async def handle_channel(event: Any) -> None:
            payload = event.payload or {}
            await self._safe_handle_message(
                mesh,
                InboundMessage(
                    text=str(payload.get("text") or ""),
                    channel_index=int(payload.get("channel_idx", -1)),
                    sender_timestamp=int(payload["sender_timestamp"])
                    if payload.get("sender_timestamp") is not None
                    else None,
                ),
            )

        async def drain_waiting(_event: Any) -> None:
            try:
                await self._drain_messages(mesh)
            except Exception as exc:
                LOG.error("Could not fetch queued mesh messages: %s", exc)

        subscriptions = [
            mesh.subscribe(EventType.ACK, remember_ack),
            mesh.subscribe(EventType.DISCONNECTED, mark_disconnected),
            mesh.subscribe(EventType.CONTACT_MSG_RECV, handle_dm),
            mesh.subscribe(
                EventType.CHANNEL_MSG_RECV,
                handle_channel,
                attribute_filters={"channel_idx": self.config.weather_channel_index},
            ),
            mesh.subscribe(EventType.MESSAGES_WAITING, drain_waiting),
            mesh.subscribe(EventType.RX_LOG_DATA, remember_rx_log),
        ]
        alert_task: Optional[asyncio.Task[Any]] = None
        try:
            await self._prepare_mesh(mesh)
            await self._drain_messages(mesh)
            alert_task = asyncio.create_task(
                self._alert_loop(mesh), name="weatherbot-alerts"
            )
            await disconnected.wait()
        finally:
            if alert_task is not None:
                alert_task.cancel()
                try:
                    await alert_task
                except asyncio.CancelledError:
                    pass
            for subscription in subscriptions:
                mesh.unsubscribe(subscription)

    async def _prepare_mesh(self, mesh: Any) -> None:
        async with self._mesh_lock:
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

            await self._refresh_contacts_unlocked(mesh)
            if not self._advertised:
                self._require_event(
                    await mesh.commands.send_advert(flood=True),
                    EventType.OK,
                    "advertising the bot",
                )
                self._advertised = True
        LOG.info("Weather bot is ready on #%s", actual)

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

    async def _safe_handle_message(self, mesh: Any, message: InboundMessage) -> None:
        try:
            await self.handle_message(mesh, message)
        except Exception as exc:
            LOG.error("Could not handle mesh message: %s", exc)

    def _request_key(self, message: InboundMessage) -> Optional[tuple]:
        if message.is_channel:
            if message.channel_index is None or message.sender_timestamp is None:
                return None
            return ("channel", message.channel_index, message.sender_timestamp, message.text)
        if not message.sender_prefix or message.sender_timestamp is None:
            return None
        return ("dm", message.sender_prefix[:12], message.sender_timestamp, message.text)

    def _associate_rx_path(self, message: InboundMessage) -> None:
        """Correlate a received DM with its RF log path and cache the reverse route."""
        if message.is_channel or not message.sender_prefix or message.path_len is None:
            return
        if message.path_len < 0:
            return
        now = time.time()
        for recv_time, path_len, path_hash_size, path in reversed(self._recent_rx_paths):
            if now - recv_time > RX_PATH_WINDOW:
                continue
            if path_len != message.path_len:
                continue
            reversed_path = reverse_mesh_path(path, path_hash_size)
            if not reversed_path:
                continue
            hash_mode = (
                message.path_hash_mode
                if message.path_hash_mode is not None
                else path_hash_size - 1
            )
            self._rx_paths[message.sender_prefix[:12]] = (reversed_path, hash_mode, now)
            LOG.info(
                "Cached newest path to %s from a received message (%d hops)",
                message.sender_prefix[:12],
                message.path_len,
            )
            return

    async def handle_message(self, mesh: Any, message: InboundMessage) -> bool:
        if message.is_channel and message.channel_index != self.config.weather_channel_index:
            return False
        zip_code = parse_wx_command(message.text, channel_message=message.is_channel)
        if zip_code is None:
            return False

        request_key = self._request_key(message)
        if request_key is not None:
            if request_key in self._seen_requests:
                LOG.info(
                    "Ignoring duplicate %s request for %s (retry or repeated packet)",
                    "channel" if message.is_channel else "DM",
                    zip_code,
                )
                return True
            self._seen_requests.append(request_key)

        LOG.info(
            "Weather request for %s via %s",
            zip_code,
            "channel" if message.is_channel else "DM",
        )
        try:
            report = await self.weather.weather_report(zip_code)
        except WeatherError as exc:
            report = f"WX {zip_code}: lookup failed: {exc}."

        if message.is_channel:
            for chunk in split_mesh_text(report):
                await self.send_channel(mesh, chunk)
            return True
        if not message.sender_prefix:
            raise MeshError("a queued DM did not include its sender key prefix")
        for chunk in split_mesh_text(report):
            await self.send_dm_with_fallback(mesh, message.sender_prefix, chunk)
        return True

    async def send_channel(self, mesh: Any, text: str) -> None:
        async with self._mesh_lock:
            self._require_event(
                await mesh.commands.send_chan_msg(
                    self.config.weather_channel_index, text
                ),
                EventType.OK,
                "sending to #Weather",
            )

    async def send_dm_with_fallback(
        self, mesh: Any, sender_prefix: str | bytes, text: str
    ) -> bool:
        """Use the newest received path (when known), retry, then reset and flood."""
        prefix = (
            sender_prefix.hex() if isinstance(sender_prefix, bytes) else sender_prefix
        ).lower()
        timestamp = int(time.time())

        async with self._mesh_lock:
            contact = await self._find_contact_unlocked(mesh, prefix)
            await self._apply_rx_path_unlocked(mesh, contact, prefix)
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

    async def _apply_rx_path_unlocked(
        self, mesh: Any, contact: dict[str, Any], prefix: str
    ) -> None:
        """Install the newest received path on the contact before a routed send."""
        entry = self._rx_paths.pop(prefix[:12], None)
        if entry is None:
            return
        path, hash_mode, _when = entry
        try:
            self._require_event(
                await mesh.commands.change_contact_path(contact, path, hash_mode),
                EventType.OK,
                "applying the newest received path",
            )
            LOG.info("DM to %s routed via the newest received path", prefix)
        except Exception as exc:
            LOG.warning("Could not apply the newest received path to %s: %s", prefix, exc)

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

    async def _find_contact_unlocked(
        self, mesh: Any, prefix: str
    ) -> dict[str, Any]:
        contact = self._contacts.get(prefix[:12])
        if contact is None:
            await self._refresh_contacts_unlocked(mesh)
            contact = self._contacts.get(prefix[:12])
        if contact is None:
            raise MeshError(f"cannot find contact for sender {prefix[:12]}")
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
        if not self.config.alert_zip_codes:
            return 0
        grouped: dict[str, tuple[dict[str, Any], list[str]]] = {}
        for zip_code in self.config.alert_zip_codes:
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
            if key in self._seen_alerts:
                continue
            try:
                for chunk in split_mesh_text(format_channel_alert(alert, zip_codes)):
                    await self.send_channel(mesh, chunk)
            except MeshError as exc:
                LOG.error("Could not send NWS alert: %s", exc)
                continue
            self._seen_alerts[key] = int(time.time())
            self._save_seen_alerts()
            sent += 1
            LOG.info("Sent new NWS alert for ZIP(s) %s", ", ".join(zip_codes))
        return sent

    def _load_seen_alerts(self) -> dict[str, int]:
        try:
            with self.config.state_file.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            seen = data.get("seen_alerts", {})
            if isinstance(seen, dict):
                return {str(key): int(value) for key, value in seen.items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            LOG.warning("Ignoring unreadable alert state: %s", exc)
        return {}

    def _save_seen_alerts(self) -> None:
        self._seen_alerts = dict(list(self._seen_alerts.items())[-2000:])
        path = self.config.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"seen_alerts": self._seen_alerts}, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)


def parse_wx_command(text: str, channel_message: bool = False) -> Optional[str]:
    match = WX_COMMAND.fullmatch(text)
    if match:
        return match.group(1)
    # openHop labels channel text as "Sender: message" for companion clients.
    if channel_message and ": " in text:
        match = WX_COMMAND.fullmatch(text.split(": ", 1)[1])
        if match:
            return match.group(1)
    return None


def normalize_zip(value: str) -> str:
    match = re.fullmatch(r"\s*(\d{5})(?:-\d{4})?\s*", str(value))
    if not match:
        raise WeatherError("expected a five-digit US ZIP code")
    return match.group(1)


def _path_len(payload: dict[str, Any]) -> Optional[int]:
    value = payload.get("path_len")
    return int(value) if value is not None else None


def _path_hash_mode(payload: dict[str, Any]) -> Optional[int]:
    value = payload.get("path_hash_mode")
    return int(value) if value is not None else None


def reverse_mesh_path(path_hex: str, hash_size: int) -> str:
    """Reverse a received mesh path into stored outbound order."""
    step = 2 * max(1, int(hash_size))
    chunks = [path_hex[i : i + step] for i in range(0, len(path_hex), step)]
    return "".join(reversed(chunks))


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
    headline = clean_text(
        str(properties.get("headline") or properties.get("description") or event)
    )
    instruction = clean_text(str(properties.get("instruction") or ""))
    timing = f", until {ends}" if ends else ""
    zips = ",".join(sorted(set(zip_codes)))
    lines = [f"🚨 NWS ALERT — {zips}", f"⚠️ {event} ({severity}, {urgency}{timing})"]
    details = headline
    if instruction and instruction.casefold() not in headline.casefold():
        details += " " + instruction
    if details:
        lines.append(details[:600].rstrip())
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
    """Split text without breaking UTF-8, leaving room for part numbers."""
    remaining = clean_lines(text)
    chunks: list[str] = []
    while remaining:
        chunk, remaining = _cut_utf8(remaining, byte_limit - 10)
        chunks.append(chunk)
    if len(chunks) <= 1:
        return chunks
    total = len(chunks)
    return [f"[{index}/{total}] {chunk}" for index, chunk in enumerate(chunks, 1)]


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
        alert_zip_codes=list(dict.fromkeys(zip_codes)),
        alert_poll_seconds=int(raw.get("alert_poll_seconds", 60)),
        direct_retries=int(raw.get("direct_retries", 3)),
        ack_timeout_seconds=float(raw.get("ack_timeout_seconds", 5)),
        reconnect_seconds=float(raw.get("reconnect_seconds", 5)),
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
    if not config.bot_name or len(config.bot_name.encode("utf-8")) > 31:
        raise SystemExit("bot_name must be 1-31 UTF-8 bytes")
    if config.alert_poll_seconds < 30:
        raise SystemExit("alert_poll_seconds must be at least 30")
    if not 0 <= config.direct_retries <= 10:
        raise SystemExit("direct_retries must be between 0 and 10")
    if config.ack_timeout_seconds <= 0 or config.reconnect_seconds <= 0:
        raise SystemExit("ack_timeout_seconds and reconnect_seconds must be positive")
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
