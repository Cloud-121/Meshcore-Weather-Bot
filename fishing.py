"""Tide, water, wind, and solunar data for compact #fishing replies."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

import httpx

COOPS_STATIONS = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
COOPS_DATA = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_MARINE = "https://marine-api.open-meteo.com/v1/marine"
STATION_CACHE_SECONDS = 24 * 3600
STATION_MAX_MILES = 50.0
WATER_MAX_AGE = timedelta(hours=6)
# Geocentric lunar rise/set altitude: parallax (0.7275 * 57') minus refraction (34').
MOON_HORIZON = 0.125
PRESSURE_TREND_INHG = 0.03
# Text that fits one mesh message alongside a "[1/2] " part number.
MESSAGE_BYTES = 130
HPA_TO_INHG = 0.0295299830714
PHASES = (
    ("New moon", "🌑"), ("Waxing crescent", "🌒"), ("First quarter", "🌓"),
    ("Waxing gibbous", "🌔"), ("Full moon", "🌕"), ("Waning gibbous", "🌖"),
    ("Last quarter", "🌗"), ("Waning crescent", "🌘"),
)
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
FISH_HELP_TEXT = (
    "🎣 fish ZIP: tides, water, wind, sun, moon, best times\n"
    "More detail: fish tide|water|wind|moon ZIP"
)


class FishingError(RuntimeError):
    """A fishing lookup failed in a way safe to report to the requester."""


class FishingService:
    """Fetch NOAA CO-OPS and Open-Meteo data and compute moon times locally."""

    def __init__(self, user_agent: str, timeout: float = 15.0,
                 application: str = "pymc-weatherbot") -> None:
        self.application = application
        self.client = httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": user_agent, "Accept": "application/json"}
        )
        self._stations: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._timezones: dict[tuple[float, float], ZoneInfo] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            raise FishingError(f"data service returned HTTP {status}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise FishingError("data service is unavailable") from exc
        if not isinstance(data, dict):
            raise FishingError("data service returned invalid data")
        if isinstance(data.get("error"), dict):
            raise FishingError(str(data["error"].get("message") or "no data"))
        return data

    async def _station_list(self, kind: str) -> list[dict[str, Any]]:
        cached = self._stations.get(kind)
        if cached and time.monotonic() - cached[0] < STATION_CACHE_SECONDS:
            return cached[1]
        data = await self._get_json(COOPS_STATIONS, {"type": kind})
        stations = [item for item in data.get("stations") or [] if isinstance(item, dict)]
        self._stations[kind] = (time.monotonic(), stations)
        return stations

    async def _coops(self, station: str, **params: str) -> dict[str, Any]:
        return await self._get_json(COOPS_DATA, {
            "station": station, "units": "english", "time_zone": "gmt",
            "format": "json", "application": self.application, **params,
        })

    async def _forecast(self, lat: float, lon: float) -> dict[str, Any]:
        data = await self._get_json(OPEN_METEO, {
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
            "current": "temperature_2m,wind_speed_10m,wind_gusts_10m,wind_direction_10m,pressure_msl",
            "hourly": "pressure_msl,wind_speed_10m,wind_gusts_10m,wind_direction_10m",
            "daily": "sunrise,sunset",
            "past_hours": "3", "forecast_hours": "7", "forecast_days": "1",
            "timezone": "auto", "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph", "timeformat": "unixtime",
        })
        if not isinstance(data.get("current"), dict):
            raise FishingError("weather service returned invalid data")
        self._timezones[(round(lat, 2), round(lon, 2))] = zone_or_utc(data.get("timezone"))
        return data

    async def _timezone(self, lat: float, lon: float) -> ZoneInfo:
        key = (round(lat, 2), round(lon, 2))
        if key not in self._timezones:
            await self._forecast(lat, lon)
        return self._timezones[key]

    async def _marine(self, lat: float, lon: float) -> dict[str, Any]:
        data = await self._get_json(OPEN_METEO_MARINE, {
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
            "current": "wave_height,wave_period,wave_direction,sea_surface_temperature",
            "length_unit": "imperial",
        })
        current = data.get("current")
        return current if isinstance(current, dict) else {}

    async def _tide_events(
        self, lat: float, lon: float, hours: int
    ) -> tuple[dict[str, Any], float, list[tuple[datetime, str, float]]]:
        found = nearest_stations(await self._station_list("tidepredictions"), lat, lon)
        if not found:
            raise FishingError(f"no tide station within {STATION_MAX_MILES:.0f} mi")
        station, miles = found[0]
        now = datetime.now(timezone.utc)
        data = await self._coops(
            str(station["id"]), product="predictions", datum="MLLW", interval="hilo",
            begin_date=now.strftime("%Y%m%d %H:%M"), range=str(hours),
        )
        events = []
        for item in data.get("predictions") or []:
            try:
                when = datetime.strptime(item["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                events.append((when, str(item["type"]).upper()[:1], float(item["v"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not events:
            raise FishingError("no tide predictions were found")
        return station, miles, events

    async def _water_temp(
        self, lat: float, lon: float
    ) -> Optional[tuple[float, datetime, dict[str, Any], float]]:
        """Return the newest reading from one of the three nearest sensors."""
        for station, miles in nearest_stations(await self._station_list("watertemp"), lat, lon)[:3]:
            try:
                data = await self._coops(str(station["id"]), product="water_temperature", date="latest")
                item = (data.get("data") or [])[0]
                when = datetime.strptime(item["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                return float(item["v"]), when, station, miles
            except (FishingError, IndexError, KeyError, TypeError, ValueError):
                continue
        return None

    async def summary(self, lat: float, lon: float, label: str) -> str:
        forecast, tides, water, marine = await asyncio.gather(
            self._forecast(lat, lon), self._tide_events(lat, lon, 24),
            self._water_temp(lat, lon), self._marine(lat, lon),
            return_exceptions=True,
        )
        if isinstance(forecast, BaseException) and isinstance(tides, BaseException):
            raise forecast if isinstance(forecast, FishingError) else FishingError(
                "data service is unavailable")
        has_forecast = not isinstance(forecast, BaseException)
        tz = await self._timezone(lat, lon) if has_forecast else ZoneInfo("UTC")
        now = datetime.now(timezone.utc)
        lines = [f"🎣 {label}"]
        if not isinstance(tides, BaseException):
            lines.append("🌊 " + " ".join(
                f"{kind} {clock(when, tz)} {height:.1f}" for when, kind, height in tides[2][:2]
            ) + "ft")
        water_parts = []
        if not isinstance(water, BaseException) and water and now - water[1] <= WATER_MAX_AGE:
            water_parts.append(f"Water {round(water[0])}°F")
        elif not isinstance(marine, BaseException):
            sst = number(marine.get("sea_surface_temperature"))
            if sst is not None:
                water_parts.append(f"Water {round(c_to_f(sst))}°F")
        if not isinstance(marine, BaseException):
            height = number(marine.get("wave_height"))
            if height is not None:
                water_parts.append(f"Waves {height:.1f}ft")
        if water_parts:
            lines.append(" · ".join(water_parts))
        if has_forecast:
            wind = wind_text(forecast["current"])
            pressure = pressure_text(forecast)
            if wind or pressure:
                lines.append("💨 " + " · ".join(part for part in (wind, pressure) if part))
        sun = sun_times(forecast, tz) if has_forecast else None
        _name, emoji, lit = moon_phase(now)
        sky = [f"🌅 {clock(sun[0], tz)} 🌇 {clock(sun[1], tz)}"] if sun else []
        lines.append(" ".join(sky + [f"{emoji} {lit}%"]))
        solunar = solunar_text(solunar_periods(now.astimezone(tz).date(), lat, lon, tz), tz)
        if solunar:
            lines.append(f"🐟 {solunar}")
        return "\n".join(lines)

    async def tides(self, lat: float, lon: float, label: str) -> str:
        station, miles, events = await self._tide_events(lat, lon, 36)
        tz = await self._timezone(lat, lon)
        lines = [f"🌊 {label}", f"{short_name(station)} {miles:.0f}mi, ft"]
        day = datetime.now(tz).date()
        for when, kind, height in events:
            local = when.astimezone(tz)
            prefix = f"{local:%a} " if local.date() != day else ""
            day = local.date()
            line = f"{prefix}{kind} {clock(when, tz)} {height:.1f}"
            if not fits(lines + [line]):
                break
            lines.append(line)
        return "\n".join(lines)

    async def water(self, lat: float, lon: float, label: str) -> str:
        water, marine = await asyncio.gather(
            self._water_temp(lat, lon), self._marine(lat, lon), return_exceptions=True
        )
        tz = await self._timezone(lat, lon)
        lines = [f"💧 {label}"]
        if not isinstance(water, BaseException) and water:
            value, when, station, miles = water
            lines.append(f"{round(value)}°F {short_name(station)} {miles:.0f}mi {clock(when, tz)}")
        elif not isinstance(marine, BaseException):
            sst = number(marine.get("sea_surface_temperature"))
            if sst is not None:
                lines.append(f"{round(c_to_f(sst))}°F sea surface")
        if not isinstance(marine, BaseException):
            waves = wave_text(marine, direction=True)
            if waves:
                lines.append(f"Waves {waves}")
        if len(lines) == 1:
            raise FishingError("no water data near this ZIP")
        return "\n".join(lines)

    async def wind(self, lat: float, lon: float, label: str) -> str:
        forecast = await self._forecast(lat, lon)
        lines = [f"💨 {label}"]
        wind = wind_text(forecast["current"])
        if wind:
            lines.append(f"Now {wind}")
        pressure = pressure_text(forecast, detail=True)
        if pressure:
            lines.append(pressure)
        now = number(forecast["current"].get("time")) or time.time()
        hourly = forecast.get("hourly") or {}
        outlook = []
        for step in (2, 4, 6):
            index = closest_index(hourly.get("time"), now + step * 3600)
            if index is None:
                continue
            values = {key: (hourly.get(key) or [None] * (index + 1))[index]
                      for key in ("wind_speed_10m", "wind_gusts_10m", "wind_direction_10m")}
            text = wind_text(values, unit="")
            if text:
                outlook.append(f"+{step}h {text}")
        lines.extend(" · ".join(outlook[i:i + 2]) for i in range(0, len(outlook), 2))
        return "\n".join(lines)

    async def moon(self, lat: float, lon: float, label: str) -> str:
        forecast = await self._forecast(lat, lon)
        tz = await self._timezone(lat, lon)
        now = datetime.now(timezone.utc)
        name, emoji, lit = moon_phase(now)
        lines = [f"🌙 {label}", f"{emoji} {name} {lit}%"]
        sun = sun_times(forecast, tz)
        if sun:
            lines.append(f"🌅 {clock(sun[0], tz)} 🌇 {clock(sun[1], tz)}")
        # Minor periods are centred on moonrise and moonset, so those times
        # are not repeated on a separate line.
        periods = solunar_periods(now.astimezone(tz).date(), lat, lon, tz)
        for kind in ("Major", "Minor"):
            times = " ".join(clock(center, tz) for center, _ in periods[kind.lower()])
            if times:
                lines.append(f"{kind} {times}")
        return "\n".join(lines)


def fits(lines: Sequence[str], limit: int = MESSAGE_BYTES) -> bool:
    return len("\n".join(lines).encode("utf-8")) <= limit


def zone_or_utc(name: Any) -> ZoneInfo:
    try:
        return ZoneInfo(str(name))
    except (ValueError, KeyError, OSError):
        return ZoneInfo("UTC")


def number(value: Any) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def c_to_f(value: float) -> float:
    return value * 9 / 5 + 32


def compass(degrees: float) -> str:
    return COMPASS[round(degrees % 360 / 45) % 8]


def clock(when: datetime, tz: ZoneInfo) -> str:
    local = when.astimezone(tz)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d}{'a' if local.hour < 12 else 'p'}"


def shorten(text: str, limit: int) -> str:
    """Cut text to limit characters, at a word boundary when there is one."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit + 1]
    return cut.rsplit(" ", 1)[0].rstrip() if " " in cut else text[:limit]


def short_name(station: dict[str, Any], limit: int = 20) -> str:
    name = str(station.get("name") or station.get("id") or "station").split(",")[0]
    return shorten(name, limit)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((phi2 - phi1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 3958.7613 * 2 * math.asin(min(1.0, math.sqrt(a)))


def nearest_stations(
    stations: Sequence[dict[str, Any]], lat: float, lon: float,
    max_miles: float = STATION_MAX_MILES,
) -> list[tuple[dict[str, Any], float]]:
    """Return stations within range, nearest first."""
    found = []
    for station in stations:
        slat, slon = number(station.get("lat")), number(station.get("lng"))
        if slat is None or slon is None or not station.get("id"):
            continue
        miles = haversine_miles(lat, lon, slat, slon)
        if miles <= max_miles:
            found.append((station, miles))
    return sorted(found, key=lambda item: item[1])


def closest_index(times: Any, target: float) -> Optional[int]:
    if not isinstance(times, list) or not times:
        return None
    values = [(abs(value - target), index) for index, value in enumerate(map(number, times))
              if value is not None]
    return min(values)[1] if values else None


def wind_text(data: dict[str, Any], unit: str = "mph") -> str:
    speed = number(data.get("wind_speed_10m"))
    if speed is None:
        return ""
    direction = number(data.get("wind_direction_10m"))
    gusts = number(data.get("wind_gusts_10m"))
    text = f"{compass(direction)} {round(speed)}" if direction is not None else f"{round(speed)}"
    if gusts is not None and round(gusts) > round(speed):
        text += f" G{round(gusts)}"
    return f"{text}{unit}"


def pressure_trend(now_hpa: float, before_hpa: float) -> tuple[str, float]:
    change = (now_hpa - before_hpa) * HPA_TO_INHG
    if change > PRESSURE_TREND_INHG:
        return "rising", change
    if change < -PRESSURE_TREND_INHG:
        return "falling", change
    return "steady", change


def pressure_text(forecast: dict[str, Any], detail: bool = False) -> str:
    current = forecast.get("current") or {}
    pressure = number(current.get("pressure_msl"))
    if pressure is None:
        return ""
    text = f"{pressure * HPA_TO_INHG:.2f}"
    if detail:
        text = f"Pressure {text}inHg"
    hourly = forecast.get("hourly") or {}
    now = number(current.get("time"))
    index = closest_index(hourly.get("time"), now - 3 * 3600) if now is not None else None
    values = hourly.get("pressure_msl") or []
    before = number(values[index]) if index is not None and index < len(values) else None
    if before is not None:
        trend, change = pressure_trend(pressure, before)
        text += f" {trend}"
        if detail:
            text += f" {change:+.2f}/3h"
    return text


def wave_text(marine: dict[str, Any], direction: bool = False) -> str:
    height = number(marine.get("wave_height"))
    if height is None:
        return ""
    text = f"{height:.1f}ft"
    period = number(marine.get("wave_period"))
    if period is not None:
        text += f" every {round(period)}s"
    heading = number(marine.get("wave_direction"))
    if direction and heading is not None:
        text += f" from {compass(heading)}"
    return text


def sun_times(forecast: dict[str, Any], tz: ZoneInfo) -> Optional[tuple[datetime, datetime]]:
    daily = forecast.get("daily") or {}
    try:
        rise = datetime.fromtimestamp(float(daily["sunrise"][0]), timezone.utc)
        sunset = datetime.fromtimestamp(float(daily["sunset"][0]), timezone.utc)
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return rise, sunset


# Low-precision lunar and solar positions after Paul Schlyter's
# "Computing planetary positions" (about two arcminutes for the Moon).

def _day_number(when: datetime) -> float:
    epoch = datetime(1999, 12, 31, tzinfo=timezone.utc)
    return (when - epoch).total_seconds() / 86400


def _kepler(mean: float, eccentricity: float) -> float:
    anomaly = mean + eccentricity * math.sin(mean) * (1 + eccentricity * math.cos(mean))
    for _ in range(5):
        anomaly -= ((anomaly - eccentricity * math.sin(anomaly) - mean)
                    / (1 - eccentricity * math.cos(anomaly)))
    return anomaly


def _sun_longitude(d: float) -> tuple[float, float, float]:
    """Return (ecliptic longitude, mean anomaly, argument of perihelion) in degrees."""
    perihelion = 282.9404 + 4.70935e-5 * d
    eccentricity = 0.016709 - 1.151e-9 * d
    mean = (356.0470 + 0.9856002585 * d) % 360
    anomaly = _kepler(math.radians(mean), eccentricity)
    xv = math.cos(anomaly) - eccentricity
    yv = math.sqrt(1 - eccentricity ** 2) * math.sin(anomaly)
    return (math.degrees(math.atan2(yv, xv)) + perihelion) % 360, mean, perihelion


def _moon_ecliptic(d: float) -> tuple[float, float]:
    """Return the Moon's geocentric ecliptic longitude and latitude in degrees."""
    node = 125.1228 - 0.0529538083 * d
    inclination = math.radians(5.1454)
    perigee = 318.0634 + 0.1643573223 * d
    eccentricity = 0.054900
    mean = (115.3654 + 13.0649929509 * d) % 360
    anomaly = _kepler(math.radians(mean), eccentricity)
    xv = math.cos(anomaly) - eccentricity
    yv = math.sqrt(1 - eccentricity ** 2) * math.sin(anomaly)
    argument = math.atan2(yv, xv) + math.radians(perigee)
    n = math.radians(node)
    xh = math.cos(n) * math.cos(argument) - math.sin(n) * math.sin(argument) * math.cos(inclination)
    yh = math.sin(n) * math.cos(argument) + math.cos(n) * math.sin(argument) * math.cos(inclination)
    zh = math.sin(argument) * math.sin(inclination)
    longitude = math.degrees(math.atan2(yh, xh))
    latitude = math.degrees(math.atan2(zh, math.hypot(xh, yh)))

    _sun, sun_mean, sun_perihelion = _sun_longitude(d)
    sun_mean_longitude = sun_mean + sun_perihelion
    moon_mean_longitude = mean + perigee + node
    elongation = math.radians(moon_mean_longitude - sun_mean_longitude)
    latitude_argument = math.radians(moon_mean_longitude - node)
    mm, ms = math.radians(mean), math.radians(sun_mean)
    dd, f = elongation, latitude_argument
    longitude += (
        -1.274 * math.sin(mm - 2 * dd) + 0.658 * math.sin(2 * dd)
        - 0.186 * math.sin(ms) - 0.059 * math.sin(2 * mm - 2 * dd)
        - 0.057 * math.sin(mm - 2 * dd + ms) + 0.053 * math.sin(mm + 2 * dd)
        + 0.046 * math.sin(2 * dd - ms) + 0.041 * math.sin(mm - ms)
        - 0.035 * math.sin(dd) - 0.031 * math.sin(mm + ms)
        - 0.015 * math.sin(2 * f - 2 * dd) + 0.011 * math.sin(mm - 4 * dd)
    )
    latitude += (
        -0.173 * math.sin(f - 2 * dd) - 0.055 * math.sin(mm - f - 2 * dd)
        - 0.046 * math.sin(mm + f - 2 * dd) + 0.033 * math.sin(f + 2 * dd)
        + 0.017 * math.sin(2 * mm + f)
    )
    return longitude % 360, latitude


def moon_equatorial(when: datetime) -> tuple[float, float]:
    """Return the Moon's geocentric right ascension and declination in degrees."""
    d = _day_number(when)
    longitude, latitude = map(math.radians, _moon_ecliptic(d))
    obliquity = math.radians(23.4393 - 3.563e-7 * d)
    x = math.cos(latitude) * math.cos(longitude)
    y = (math.cos(obliquity) * math.cos(latitude) * math.sin(longitude)
         - math.sin(obliquity) * math.sin(latitude))
    z = (math.sin(obliquity) * math.cos(latitude) * math.sin(longitude)
         + math.cos(obliquity) * math.sin(latitude))
    return math.degrees(math.atan2(y, x)) % 360, math.degrees(math.atan2(z, math.hypot(x, y)))


def moon_horizontal(when: datetime, lat: float, lon: float) -> tuple[float, float]:
    """Return (altitude, hour angle in -180..180) of the Moon in degrees."""
    ra, dec = moon_equatorial(when)
    julian = when.timestamp() / 86400 + 2440587.5
    sidereal = (280.46061837 + 360.98564736629 * (julian - 2451545.0)) % 360
    hour_angle = (sidereal + lon - ra + 180) % 360 - 180
    phi, delta, h = map(math.radians, (lat, dec, hour_angle))
    altitude = math.asin(math.sin(phi) * math.sin(delta)
                         + math.cos(phi) * math.cos(delta) * math.cos(h))
    return math.degrees(altitude), hour_angle


def moon_phase(when: datetime) -> tuple[str, str, int]:
    """Return phase name, emoji, and illuminated percentage."""
    d = _day_number(when)
    moon_longitude, _ = _moon_ecliptic(d)
    sun_longitude, _, _ = _sun_longitude(d)
    age = (moon_longitude - sun_longitude) % 360
    illumination = round((1 - math.cos(math.radians(age))) / 2 * 100)
    name, emoji = PHASES[round(age / 45) % 8]
    return name, emoji, illumination


def moon_events(
    day: date, lat: float, lon: float, tz: ZoneInfo, step_minutes: int = 10
) -> dict[str, list[datetime]]:
    """Find moonrise, moonset, and upper/lower transits during a local calendar day."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)
    end = (datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(days=1)).astimezone(timezone.utc)
    events: dict[str, list[datetime]] = {"rise": [], "set": [], "upper": [], "lower": []}
    step = timedelta(minutes=step_minutes)
    previous_time = start
    previous_alt, previous_ha = moon_horizontal(start, lat, lon)
    while previous_time < end:
        current_time = min(previous_time + step, end)
        altitude, hour_angle = moon_horizontal(current_time, lat, lon)
        span = (current_time - previous_time).total_seconds()

        def at(fraction: float) -> datetime:
            return previous_time + timedelta(seconds=span * fraction)

        low, high = previous_alt - MOON_HORIZON, altitude - MOON_HORIZON
        if low < 0 <= high:
            events["rise"].append(at(-low / (high - low)))
        elif low >= 0 > high:
            events["set"].append(at(low / (low - high)))
        if previous_ha < 0 <= hour_angle:
            events["upper"].append(at(-previous_ha / (hour_angle - previous_ha)))
        elif previous_ha > 90 and hour_angle < -90:
            unwrapped = hour_angle + 360
            events["lower"].append(at((180 - previous_ha) / (unwrapped - previous_ha)))
        previous_time, previous_alt, previous_ha = current_time, altitude, hour_angle
    return events


def solunar_periods(
    day: date, lat: float, lon: float, tz: ZoneInfo,
    events: Optional[dict[str, list[datetime]]] = None,
) -> dict[str, list[tuple[datetime, timedelta]]]:
    """Major periods are lunar transits +/-1h; minor periods are rise/set +/-30min."""
    events = events or moon_events(day, lat, lon, tz)
    major = sorted(events["upper"] + events["lower"])
    minor = sorted(events["rise"] + events["set"])
    return {
        "major": [(center, timedelta(hours=1)) for center in major],
        "minor": [(center, timedelta(minutes=30)) for center in minor],
    }


def solunar_text(periods: dict[str, list[tuple[datetime, timedelta]]], tz: ZoneInfo) -> str:
    parts = []
    for kind in ("major", "minor"):
        times = " ".join(clock(center, tz) for center, _ in periods[kind])
        if times:
            parts.append(f"{kind.title()} {times}")
    return " ".join(parts)
