"""NOAA observed and forecast reflectivity service for compact mesh replies."""

from __future__ import annotations

import asyncio
import gzip
import math
import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryFile
from typing import Any, Optional
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

import radar_codec

MRMS_BUCKET = "https://noaa-mrms-pds.s3.amazonaws.com"
MRMS_PREFIX = "CONUS/MergedReflectivityQCComposite_00.50"
HRRR_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_hrrr_2d.pl"
_MRMS_TIME = re.compile(r"_(\d{8})-(\d{6})\.grib2\.gz$")
_REQUEST = re.compile(
    r"wx\s+radar\s+(\S+)\s+(\S+)\s+(now|[+-][0-5]h)", re.IGNORECASE
)


class RadarError(RuntimeError):
    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def parse_radar_request(command: str) -> Optional[tuple[float, float, int]]:
    match = _REQUEST.fullmatch(command)
    if not match:
        return None
    try:
        latitude, longitude = float(match.group(1)), float(match.group(2))
    except ValueError as exc:
        raise RadarError("invalid coordinates", 5) from exc
    if not math.isfinite(latitude) or not math.isfinite(longitude) \
            or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise RadarError("invalid coordinates", 5)
    selection = match.group(3).lower()
    offset = 0 if selection == "now" else int(selection[:-1])
    return latitude, longitude, offset


def _sample_points(latitude: float, longitude: float, width: int) -> tuple[list[float], list[float]]:
    radius_km = radar_codec.RADIUS_MILES * 1.609344
    earth_km = 6371.0088
    lat1, lon1 = math.radians(latitude), math.radians(longitude)
    latitudes, longitudes = [], []
    for x, y in radar_codec.circle_cells(width):
        east = ((x + 0.5) / width * 2 - 1) * radius_km
        north = (1 - (y + 0.5) / width * 2) * radius_km
        distance = math.hypot(east, north) / earth_km
        bearing = math.atan2(east, north)
        lat2 = math.asin(math.sin(lat1) * math.cos(distance)
                         + math.cos(lat1) * math.sin(distance) * math.cos(bearing))
        lon2 = lon1 + math.atan2(
            math.sin(bearing) * math.sin(distance) * math.cos(lat1),
            math.cos(distance) - math.sin(lat1) * math.sin(lat2),
        )
        latitudes.append(math.degrees(lat2))
        longitudes.append((math.degrees(lon2) + 180) % 360 - 180)
    return latitudes, longitudes


def _quantize_reflectivity(value: Any) -> int:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 7
    if not math.isfinite(value) or value <= -90 or value >= 900:
        return 7
    for code, threshold in enumerate((5, 20, 30, 40, 50, 60)):
        if value < threshold:
            return code
    return 6


def _regular_grid_values(handle: Any, latitudes: list[float],
                         longitudes: list[float]) -> list[float]:
    """Sample a row-major lat/lon grid with one packed-data extraction.

    MRMS uses PNG packing: ecCodes' nearest-point API unpacks the whole
    CONUS field repeatedly. Compute the four surrounding grid points here,
    select by spherical distance, then ask ecCodes for all values at once.
    """
    from eccodes import codes_get, codes_get_elements

    ni, nj = codes_get(handle, "Ni"), codes_get(handle, "Nj")
    lat0 = codes_get(handle, "latitudeOfFirstGridPointInDegrees")
    lon0 = codes_get(handle, "longitudeOfFirstGridPointInDegrees")
    di = codes_get(handle, "iDirectionIncrementInDegrees")
    dj = codes_get(handle, "jDirectionIncrementInDegrees")
    if codes_get(handle, "iScansNegatively"):
        di = -di
    if not codes_get(handle, "jScansPositively"):
        dj = -dj
    if ni < 2 or nj < 2 or not di or not dj:
        raise ValueError("invalid radar grid geometry")

    indexes = []
    for lat, lon in zip(latitudes, longitudes, strict=True):
        # Longitude encodings may use either -180..180 or 0..360.
        delta_lon = (lon - lon0) % 360 if di > 0 else -((lon0 - lon) % 360)
        x, y = delta_lon / di, (lat - lat0) / dj
        if not -1e-6 <= x <= ni - 1 + 1e-6 or not -1e-6 <= y <= nj - 1 + 1e-6:
            raise ValueError("sample point is outside radar grid")
        x, y = max(0, min(ni - 1, x)), max(0, min(nj - 1, y))
        candidates = []
        for row in {math.floor(y), math.ceil(y)}:
            grid_lat = math.radians(lat0 + row * dj)
            for col in {math.floor(x), math.ceil(x)}:
                dlat = grid_lat - math.radians(lat)
                dlon = math.radians(lon0 + col * di - lon)
                distance = (math.sin(dlat / 2) ** 2
                            + math.cos(math.radians(lat)) * math.cos(grid_lat)
                            * math.sin(dlon / 2) ** 2)
                candidates.append((distance, row * ni + col))
        indexes.append(min(candidates)[1])
    return list(codes_get_elements(handle, "values", indexes))


def _decode_grib(content: bytes, compressed: bool, latitudes: list[float],
                 longitudes: list[float]) -> list[int]:
    try:
        from eccodes import (codes_get, codes_grib_find_nearest_multiple,
                             codes_grib_new_from_file, codes_release)
    except ImportError as exc:
        raise RadarError("radar decoder is not installed") from exc
    try:
        raw = gzip.decompress(content) if compressed else content
        with TemporaryFile() as stream:
            stream.write(raw)
            stream.seek(0)
            handle = codes_grib_new_from_file(stream)
            if handle is None:
                raise ValueError("no GRIB field")
            try:
                if (codes_get(handle, "gridType") == "regular_ll"
                        and not codes_get(handle, "jPointsAreConsecutive")
                        and not codes_get(handle, "alternativeRowScanning")):
                    values = _regular_grid_values(handle, latitudes, longitudes)
                    return [_quantize_reflectivity(value) for value in values]
                nearest = codes_grib_find_nearest_multiple(
                    handle, False, latitudes, longitudes
                )
                return [_quantize_reflectivity(item["value"]) for item in nearest]
            finally:
                codes_release(handle)
    except RadarError:
        raise
    except Exception as exc:
        raise RadarError("radar service returned invalid GRIB data", 3) from exc


class RadarService:
    """Retrieve one MRMS observation or HRRR simulated-reflectivity forecast."""

    def __init__(self, user_agent: str, timeout: float = 30.0,
                 client: Optional[httpx.AsyncClient] = None) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": user_agent}
        )
        self._listings: dict[str, tuple[float, list[tuple[datetime, str]]]] = {}
        self._files: OrderedDict[str, bytes] = OrderedDict()

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def snapshot(self, latitude: float, longitude: float, offset: int,
                       now: Optional[datetime] = None) -> dict:
        if not -5 <= offset <= 5:
            raise RadarError("radar time must be between -5h and +5h", 5)
        # The initial provider profile is explicitly CONUS-only.
        if not 20 <= latitude <= 55 or not -135 <= longitude <= -60:
            raise RadarError("radar location is outside CONUS coverage", 6)
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        width = 32
        latitudes, longitudes = _sample_points(latitude, longitude, width)
        if offset <= 0:
            valid, content = await self._mrms_frame(now + timedelta(hours=offset))
            cells = await asyncio.to_thread(
                _decode_grib, content, True, latitudes, longitudes
            )
            return {"k": "r", "lat": latitude, "lon": longitude, "o": offset,
                    "v": int(valid.timestamp()), "s": "observed", "n": width,
                    "c": cells}
        valid, issued, content = await self._hrrr_frame(
            latitude, longitude, now + timedelta(hours=offset), now
        )
        cells = await asyncio.to_thread(
            _decode_grib, content, False, latitudes, longitudes
        )
        return {"k": "r", "lat": latitude, "lon": longitude, "o": offset,
                "v": int(valid.timestamp()), "i": int(issued.timestamp()),
                "s": "forecast", "n": width, "c": cells}

    async def _get(self, url: str, params: Optional[dict[str, str]] = None) -> bytes:
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RadarError("radar service is unavailable") from exc
        return response.content

    async def _listing(self, day: str) -> list[tuple[datetime, str]]:
        loop_time = asyncio.get_running_loop().time()
        cached = self._listings.get(day)
        if cached and loop_time - cached[0] < 120:
            return cached[1]
        prefix = f"{MRMS_PREFIX}/{day}/"
        content = await self._get(MRMS_BUCKET, {
            "list-type": "2", "prefix": prefix, "max-keys": "1000"
        })
        try:
            root = ElementTree.fromstring(content)
            frames = []
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] != "Key" or not element.text:
                    continue
                match = _MRMS_TIME.search(element.text)
                if match:
                    stamp = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                    frames.append((stamp, element.text))
        except (ValueError, ElementTree.ParseError) as exc:
            raise RadarError("radar listing was invalid", 3) from exc
        frames.sort()
        self._listings[day] = (loop_time, frames)
        return frames

    async def _mrms_frame(self, target: datetime) -> tuple[datetime, bytes]:
        frames = await self._listing(target.strftime("%Y%m%d"))
        candidates = [frame for frame in frames if frame[0] <= target]
        if not candidates and target.hour == 0:
            previous = (target - timedelta(days=1)).strftime("%Y%m%d")
            candidates = await self._listing(previous)
        if not candidates or target - candidates[-1][0] > timedelta(minutes=15):
            raise RadarError("no radar observation is available for that time", 7)
        valid, key = candidates[-1]
        return valid, await self._cached_file(
            key, f"{MRMS_BUCKET}/{quote(key, safe='/')}"
        )

    async def _cached_file(self, key: str, url: str,
                           params: Optional[dict[str, str]] = None) -> bytes:
        if key in self._files:
            self._files.move_to_end(key)
            return self._files[key]
        content = await self._get(url, params)
        self._files[key] = content
        while len(self._files) > 16:
            self._files.popitem(last=False)
        return content

    async def _hrrr_frame(self, latitude: float, longitude: float,
                          desired: datetime, now: datetime
                          ) -> tuple[datetime, datetime, bytes]:
        valid = desired.replace(minute=0, second=0, microsecond=0)
        if desired.minute >= 30:
            valid += timedelta(hours=1)
        cycle = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
        lat_delta = 50 / 69 + 0.1
        lon_delta = 50 / (69.172 * max(0.2, math.cos(math.radians(latitude)))) + 0.1
        for age in range(5):
            issued = cycle - timedelta(hours=age)
            forecast_hour = round((valid - issued).total_seconds() / 3600)
            if not 1 <= forecast_hour <= 18:
                continue
            filename = f"hrrr.t{issued:%H}z.wrfsfcf{forecast_hour:02d}.grib2"
            params = {
                "dir": f"/hrrr.{issued:%Y%m%d}/conus", "file": filename,
                "var_REFC": "on", "lev_entire_atmosphere": "on", "subregion": "",
                "leftlon": f"{longitude - lon_delta:.4f}",
                "rightlon": f"{longitude + lon_delta:.4f}",
                "toplat": f"{latitude + lat_delta:.4f}",
                "bottomlat": f"{latitude - lat_delta:.4f}",
            }
            key = f"{issued.isoformat()}/{filename}/{latitude:.3f}/{longitude:.3f}"
            try:
                content = await self._cached_file(key, HRRR_FILTER, params)
            except RadarError:
                continue
            if content.startswith(b"GRIB"):
                return valid, issued, content
            self._files.pop(key, None)
        raise RadarError("no radar forecast is available for that time", 7)
