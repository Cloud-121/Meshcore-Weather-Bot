import time
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

import fishing
import weatherbot

GRAND_ISLE = (29.2366, -89.9873)
CHICAGO = ZoneInfo("America/Chicago")


def coops_time(when):
    return when.strftime("%Y-%m-%d %H:%M")


class FakeFishingApis:
    """Serve CO-OPS and Open-Meteo responses relative to the current time."""

    def __init__(self, coastal=True, water_missing_first=False):
        self.coastal = coastal
        self.water_missing_first = water_missing_first
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        params = dict(request.url.params)
        now = datetime.now(timezone.utc)
        if request.url.path.endswith("stations.json"):
            stations = [
                {"id": "8761724", "name": "Grand Isle, East Point", "lat": 29.2633, "lng": -89.9573},
                {"id": "8761305", "name": "Shell Beach", "lat": 29.8681, "lng": -89.6733},
                {"id": "9999999", "name": "Far Away", "lat": 40.0, "lng": -70.0},
                {"id": "", "name": "Broken", "lat": "x", "lng": None},
            ]
            if params["type"] == "watertemp" and self.water_missing_first:
                stations.insert(0, {"id": "8761000", "name": "Dead Sensor", "lat": 29.24, "lng": -89.99})
            return httpx.Response(200, json={"stations": stations})
        if request.url.path.endswith("datagetter"):
            if params["product"] == "predictions":
                events = [
                    {"t": coops_time(now + timedelta(hours=2 + 6 * index)),
                     "v": f"{1.3 if index % 2 == 0 else 0.2:.3f}",
                     "type": "H" if index % 2 == 0 else "L"}
                    for index in range(6)
                ]
                return httpx.Response(200, json={"predictions": events})
            if params["station"] == "8761000":
                return httpx.Response(200, json={"error": {"message": "No data was found."}})
            return httpx.Response(200, json={"data": [
                {"t": coops_time(now - timedelta(minutes=30)), "v": "78.4", "f": "0,0,0"}
            ]})
        if request.url.host == "marine-api.open-meteo.com":
            if not self.coastal:
                return httpx.Response(200, json={"current": {
                    "wave_height": None, "wave_period": None,
                    "wave_direction": None, "sea_surface_temperature": None,
                }})
            return httpx.Response(200, json={"current": {
                "wave_height": 2.13, "wave_period": 5.2,
                "wave_direction": 135, "sea_surface_temperature": 26.0,
            }})
        if request.url.host == "api.open-meteo.com":
            current = int(time.time())
            return httpx.Response(200, json={
                "timezone": "America/Chicago",
                "current": {
                    "time": current, "temperature_2m": 82.4, "wind_speed_10m": 12.2,
                    "wind_gusts_10m": 18.1, "wind_direction_10m": 140, "pressure_msl": 1011.0,
                },
                "hourly": {
                    "time": [current + hour * 3600 for hour in range(-3, 7)],
                    "pressure_msl": [1013.0] + [1012.0] * 9,
                    "wind_speed_10m": [10, 10, 11, 12, 13, 14, 15, 16, 17, 18],
                    "wind_gusts_10m": [15, 15, 16, 18, 19, 20, 21, 22, 23, 24],
                    "wind_direction_10m": [140] * 10,
                },
                "daily": {"sunrise": [current - 3600], "sunset": [current + 3600]},
            })
        raise AssertionError(f"unexpected request {request.url}")


def make_service(apis):
    service = fishing.FishingService("fishing-tests (tests@example.com)", timeout=1)
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(apis.handler))
    return service


class AstronomyTests(unittest.TestCase):
    def test_phases_on_known_2024_dates(self):
        for when, name, low, high in (
            ("2024-04-08T18:21", "New moon", 0, 1),
            ("2024-04-15T19:13", "First quarter", 47, 53),
            ("2024-04-23T23:49", "Full moon", 99, 100),
            ("2024-05-01T11:27", "Last quarter", 47, 53),
        ):
            with self.subTest(when=when):
                phase, _emoji, lit = fishing.moon_phase(
                    datetime.fromisoformat(when).replace(tzinfo=timezone.utc)
                )
                self.assertEqual(phase, name)
                self.assertGreaterEqual(lit, low)
                self.assertLessEqual(lit, high)

    def test_eclipse_day_transit_matches_solar_noon(self):
        # At the 2024-04-08 conjunction the Moon sits beside the Sun, so it
        # transits New Orleans within minutes of solar noon (about 18:02 UTC).
        events = fishing.moon_events(date(2024, 4, 8), 29.95, -90.07, CHICAGO)
        self.assertEqual(len(events["upper"]), 1)
        transit = events["upper"][0].astimezone(timezone.utc)
        noon = datetime(2024, 4, 8, 18, 2, tzinfo=timezone.utc)
        self.assertLess(abs(transit - noon), timedelta(minutes=15))
        # Rise is near sunrise (about 6:43a CDT) and set near sunset (7:28p).
        self.assertLess(abs(events["rise"][0] - datetime(2024, 4, 8, 6, 43, tzinfo=CHICAGO)), timedelta(minutes=30))
        self.assertLess(abs(events["set"][0] - datetime(2024, 4, 8, 19, 28, tzinfo=CHICAGO)), timedelta(minutes=30))

    def test_events_are_self_consistent(self):
        events = fishing.moon_events(date(2026, 10, 19), 29.95, -90.07, CHICAGO)
        for when in events["upper"]:
            self.assertLess(abs(fishing.moon_horizontal(when, 29.95, -90.07)[1]), 0.5)
        for when in events["lower"]:
            self.assertGreater(abs(fishing.moon_horizontal(when, 29.95, -90.07)[1]), 179.5)
        for when in events["rise"] + events["set"]:
            altitude = fishing.moon_horizontal(when, 29.95, -90.07)[0]
            self.assertAlmostEqual(altitude, fishing.MOON_HORIZON, delta=0.2)
        periods = fishing.solunar_periods(date(2026, 10, 19), 29.95, -90.07, CHICAGO, events)
        self.assertEqual(len(periods["major"]), len(events["upper"]) + len(events["lower"]))
        self.assertTrue(all(width == timedelta(hours=1) for _, width in periods["major"]))
        self.assertTrue(all(width == timedelta(minutes=30) for _, width in periods["minor"]))

    def test_transits_advance_about_fifty_minutes_a_day(self):
        first = fishing.moon_events(date(2026, 10, 19), 29.95, -90.07, CHICAGO)["upper"][0]
        second = fishing.moon_events(date(2026, 10, 20), 29.95, -90.07, CHICAGO)["upper"][0]
        self.assertLess(abs((second - first) - timedelta(hours=24, minutes=50)), timedelta(minutes=20))


class HelperTests(unittest.TestCase):
    def test_nearest_station_and_range_limit(self):
        stations = [
            {"id": "far", "lat": 30.9, "lng": -90.0},
            {"id": "near", "lat": 29.27, "lng": -89.96},
            {"id": "bad", "lat": None, "lng": -90},
        ]
        found = fishing.nearest_stations(stations, *GRAND_ISLE)
        self.assertEqual([item[0]["id"] for item in found], ["near"])
        self.assertLess(found[0][1], 5)
        self.assertEqual(fishing.nearest_stations(stations, 41.88, -87.62), [])

    def test_pressure_trend_thresholds(self):
        one_hundredth = 0.01 / fishing.HPA_TO_INHG
        self.assertEqual(fishing.pressure_trend(1012 + 4 * one_hundredth, 1012)[0], "rising")
        self.assertEqual(fishing.pressure_trend(1012 + 2 * one_hundredth, 1012)[0], "steady")
        self.assertEqual(fishing.pressure_trend(1012 - 2 * one_hundredth, 1012)[0], "steady")
        self.assertEqual(fishing.pressure_trend(1012 - 4 * one_hundredth, 1012)[0], "falling")

    def test_text_helpers(self):
        self.assertEqual(fishing.clock(datetime(2026, 1, 1, 0, 5, tzinfo=CHICAGO), CHICAGO), "12:05a")
        self.assertEqual(fishing.clock(datetime(2026, 1, 1, 14, 14, tzinfo=CHICAGO), CHICAGO), "2:14p")
        self.assertEqual(fishing.wind_text({"wind_speed_10m": 12, "wind_gusts_10m": 12, "wind_direction_10m": 0}), "N 12mph")
        self.assertEqual(fishing.wave_text({"wave_height": None}), "")
        self.assertEqual(fishing.short_name({"name": "Grand Isle, East Point"}), "Grand Isle")
        self.assertEqual(fishing.short_name({"name": "Stouts Pass at Six Mile Lake"}), "Stouts Pass at Six")
        self.assertEqual(fishing.short_name({"name": "x" * 30}), "x" * 20)


class FishingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        if hasattr(self, "service"):
            await self.service.close()

    async def test_coastal_summary(self):
        self.service = make_service(FakeFishingApis())
        text = await self.service.summary(*GRAND_ISLE, "Grand Isle, LA 70357")
        self.assertIn("🎣 Grand Isle, LA 70357", text)
        self.assertIn("H ", text)
        self.assertIn("1.3ft · L ", text)
        self.assertIn("(Grand Isle)", text)
        self.assertIn("Water 78°F", text)
        self.assertIn("Air 82°F", text)
        self.assertIn("Waves 2.1ft 5s", text)
        self.assertIn("SE 12 G18mph", text)
        self.assertIn('29.85" falling', text)
        self.assertIn("🐟 Major", text)
        for chunk in weatherbot.split_mesh_text(text):
            self.assertLessEqual(len(chunk.encode()), 140)

    async def test_inland_summary_omits_tides_and_waves(self):
        self.service = make_service(FakeFishingApis(coastal=False))
        text = await self.service.summary(41.88, -87.62, "Chicago, IL 60601")
        self.assertNotIn("🌊", text)
        self.assertNotIn("Waves", text)
        self.assertNotIn("Water", text)
        self.assertIn("Air 82°F", text)
        with self.assertRaisesRegex(fishing.FishingError, "no tide station within 50 mi"):
            await self.service.tides(41.88, -87.62, "Chicago, IL 60601")
        with self.assertRaisesRegex(fishing.FishingError, "no water data"):
            await self.service.water(41.88, -87.62, "Chicago, IL 60601")

    async def test_station_lists_are_cached(self):
        apis = FakeFishingApis()
        self.service = make_service(apis)
        await self.service.tides(*GRAND_ISLE, "Grand Isle")
        await self.service.tides(*GRAND_ISLE, "Grand Isle")
        lists = [r for r in apis.requests if r.url.path.endswith("stations.json")]
        self.assertEqual(len(lists), 1)

    async def test_subcommands(self):
        apis = FakeFishingApis(water_missing_first=True)
        self.service = make_service(apis)
        tides = await self.service.tides(*GRAND_ISLE, "Grand Isle, LA 70357")
        self.assertEqual(tides.count("ft"), 6)
        self.assertIn("MLLW", tides)
        water = await self.service.water(*GRAND_ISLE, "Grand Isle, LA 70357")
        # The nearest sensor has no data, so the next one is used.
        self.assertIn("78.4°F Grand Isle", water)
        self.assertIn("Sea surface 79°F", water)
        self.assertIn("Waves 2.1ft 5s from SE", water)
        wind = await self.service.wind(*GRAND_ISLE, "Grand Isle, LA 70357")
        self.assertIn("Now SE 12 G18mph", wind)
        self.assertIn('Pressure 29.85" falling (-0.06/3h)', wind)
        self.assertIn("+6h SE 18 G24mph", wind)
        moon = await self.service.moon(*GRAND_ISLE, "Grand Isle, LA 70357")
        self.assertIn("Sun ", moon)
        self.assertIn("Major ", moon)
        for text in (tides, water, wind, moon):
            for chunk in weatherbot.split_mesh_text(text):
                self.assertLessEqual(len(chunk.encode()), 140)

    async def test_service_errors_are_reportable(self):
        self.service = fishing.FishingService("fishing-tests", timeout=1)
        self.service.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        )
        with self.assertRaisesRegex(fishing.FishingError, "HTTP 503"):
            await self.service.summary(*GRAND_ISLE, "Grand Isle")


if __name__ == "__main__":
    unittest.main()
