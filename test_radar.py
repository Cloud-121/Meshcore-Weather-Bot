"""Offline GRIB sampling regressions using small real ecCodes fields."""

import gzip
import random
import unittest
from unittest.mock import patch

import eccodes

import radar


def regular_field(i_negative=False, j_positive=False, bitmap=False):
    handle = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    settings = {
        "Ni": 7, "Nj": 5,
        "latitudeOfFirstGridPointInDegrees": 30 if j_positive else 34,
        "latitudeOfLastGridPointInDegrees": 34 if j_positive else 30,
        "longitudeOfFirstGridPointInDegrees": 272 if i_negative else 266,
        "longitudeOfLastGridPointInDegrees": 266 if i_negative else 272,
        "iDirectionIncrementInDegrees": 1,
        "jDirectionIncrementInDegrees": 1,
        "iScansNegatively": int(i_negative),
        "jScansPositively": int(j_positive),
        # Leave enough encoder buffer for PNG headers on this tiny fixture.
        "bitsPerValue": 32,
    }
    try:
        for key, value in settings.items():
            eccodes.codes_set(handle, key, value)
        values = [(-99, 0, 10, 25, 35, 45, 55, 65)[i % 8] for i in range(35)]
        if bitmap:
            eccodes.codes_set(handle, "missingValue", 9999)
            eccodes.codes_set(handle, "bitmapPresent", 1)
            values[10] = 9999
        eccodes.codes_set_values(handle, values)
        eccodes.codes_set(handle, "packingType", "grid_png")
        return eccodes.codes_get_message(handle)
    finally:
        eccodes.codes_release(handle)


class RadarDecodeTests(unittest.TestCase):
    def test_png_bulk_sampling_matches_native_nearest(self):
        rng = random.Random(413)
        latitudes = [rng.uniform(30.01, 33.99) for _ in range(80)]
        longitudes = [rng.uniform(-93.99, -88.01) for _ in range(80)]
        for i_negative, j_positive in [(False, False), (True, False),
                                        (False, True), (True, True)]:
            with self.subTest(i_negative=i_negative, j_positive=j_positive):
                content = regular_field(i_negative, j_positive)
                handle = eccodes.codes_new_from_message(content)
                try:
                    expected = [radar._quantize_reflectivity(p["value"])
                                for p in eccodes.codes_grib_find_nearest_multiple(
                                    handle, False, latitudes, longitudes)]
                finally:
                    eccodes.codes_release(handle)
                with patch("eccodes.codes_get_elements", wraps=eccodes.codes_get_elements) as bulk:
                    actual = radar._decode_grib(gzip.compress(content), True,
                                                latitudes, longitudes)
                self.assertEqual(actual, expected)
                bulk.assert_called_once()
                self.assertEqual(len(bulk.call_args.args[2]), len(latitudes))

    def test_grid_centers_boundaries_and_longitude_encodings(self):
        content = regular_field()
        latitudes = [34 - row for row in range(5) for col in range(7)]
        longitudes = [266 + col for row in range(5) for col in range(7)]
        expected = [7, 0, 1, 2, 3, 4, 5, 6] * 4 + [7, 0, 1]
        for longitude_offset in (0, -360):
            self.assertEqual(radar._decode_grib(
                content, False, latitudes,
                [lon + longitude_offset for lon in longitudes]), expected)

    def test_bitmap_missing_cell_is_unknown(self):
        self.assertEqual(radar._decode_grib(regular_field(bitmap=True), False,
                                          [33, 33], [-91, -90]), [7, 2])

    def test_other_grids_keep_native_sampler(self):
        handle = eccodes.codes_grib_new_from_samples("reduced_gg_pl_32_grib2")
        try:
            content = eccodes.codes_get_message(handle)
            expected = [radar._quantize_reflectivity(p["value"])
                        for p in eccodes.codes_grib_find_nearest_multiple(
                            handle, False, [30.45], [-91.18])]
        finally:
            eccodes.codes_release(handle)
        with patch("eccodes.codes_grib_find_nearest_multiple",
                   wraps=eccodes.codes_grib_find_nearest_multiple) as nearest:
            self.assertEqual(radar._decode_grib(content, False, [30.45], [-91.18]), expected)
        nearest.assert_called_once()

    def test_invalid_data_and_out_of_grid_are_radar_errors(self):
        for content, compressed, lat, lon in [
            (b"not gzip", True, 32, -91),
            (b"not GRIB", False, 32, -91),
            (regular_field(), False, 50, -91),
        ]:
            with self.subTest(compressed=compressed, latitude=lat):
                with self.assertRaises(radar.RadarError) as caught:
                    radar._decode_grib(content, compressed, [lat], [lon])
                self.assertEqual(caught.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
