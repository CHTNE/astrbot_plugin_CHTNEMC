from __future__ import annotations

import math
import unittest

from core import (
    describe_daytime,
    marker_distance,
    markers_from_dynmap,
    nearest_marker,
    normalize_command,
    parse_daytime,
    parse_dimension,
    parse_online_list,
    parse_position,
)


class ParsingTests(unittest.TestCase):
    def test_online_list(self):
        result = parse_online_list(
            "There are 2 of a max of 20 players online: M42_Thyphein, Steve"
        )
        self.assertEqual((result.online, result.maximum), (2, 20))
        self.assertEqual(result.players, ["M42_Thyphein", "Steve"])

    def test_empty_online_list(self):
        result = parse_online_list("There are 0 of a max of 20 players online:")
        self.assertEqual(result.players, [])
        self.assertEqual(result.online, 0)

    def test_entity_values(self):
        self.assertEqual(
            parse_position(
                "M42_Thyphein has the following entity data: [-120.25d, 68.0d, -235.75d]"
            ),
            (-120.25, 68.0, -235.75),
        )
        self.assertEqual(
            parse_dimension('M42_Thyphein has the following entity data: "minecraft:overworld"'),
            "minecraft:overworld",
        )

    def test_time(self):
        self.assertEqual(parse_daytime("The time is 6000"), 6000)
        self.assertEqual(describe_daytime(0), ("06:00", "早晨"))
        self.assertEqual(describe_daytime(6000), ("12:00", "中午"))
        self.assertEqual(describe_daytime(13000), ("19:00", "晚上"))

    def test_command_validation(self):
        self.assertEqual(normalize_command("/Weather"), "weather")
        self.assertIsNone(normalize_command("say\nstop"))


class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.markers = markers_from_dynmap(
            {
                "sets": {
                    "markers": {
                        "markers": {
                            "farm": {"label": "<b>刷怪塔</b>", "x": -100, "z": -200}
                        },
                        "circles": {
                            "spawn": {
                                "label": "出生区",
                                "x": 0,
                                "z": 0,
                                "xr": 50,
                                "zr": 50,
                            }
                        },
                        "areas": {
                            "town": {
                                "label": "城镇",
                                "x": [100, 200, 200, 100],
                                "z": [100, 100, 200, 200],
                            }
                        },
                    }
                }
            }
        )

    def test_point_marker(self):
        self.assertEqual(nearest_marker(self.markers, -120, -235, 100), "刷怪塔")

    def test_circle_and_area(self):
        circle = next(marker for marker in self.markers if marker.label == "出生区")
        area = next(marker for marker in self.markers if marker.label == "城镇")
        self.assertEqual(marker_distance(circle, 25, 25), 0)
        self.assertEqual(marker_distance(area, 150, 150), 0)
        self.assertTrue(math.isinf(marker_distance(area, 250, 250)))


if __name__ == "__main__":
    unittest.main()
