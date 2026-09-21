"""Road-provider contract tests; no external requests or private coordinates."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

import requests
from road_directions import DirectionsError, RoadDirections, instruction


def road_payload(legs=1):
    return {"code": "Ok", "routes": [{"distance": 1200, "duration": 240,
        "geometry": {"type": "LineString", "coordinates": [[77.4, 23.2], [77.401, 23.201], [77.402, 23.2]]},
        "legs": [{"distance": 1200, "duration": 240, "steps": [
            {"distance": 1100, "duration": 220, "name": "Market Road",
             "maneuver": {"type": "turn", "modifier": "left", "location": [77.401, 23.201]}},
            {"distance": 100, "duration": 20, "name": "",
             "maneuver": {"type": "arrive", "modifier": "right", "location": [77.402, 23.2]}}
        ]} for _ in range(legs)]}]}


def response(payload=None, status=200):
    result = Mock(status_code=status)
    result.json.return_value = road_payload() if payload is None else payload
    return result


class RoadDirectionsTests(unittest.TestCase):
    def setUp(self):
        self.get = Mock(return_value=response())
        self.now = 100.0
        self.router = RoadDirections("https://routing.example.test", http_get=self.get, clock=lambda: self.now)
        self.points = [[77.4, 23.2], [77.402, 23.2]]

    def test_uses_driving_geometry_in_correct_coordinate_order(self):
        result = self.router.route(self.points)
        args, kwargs = self.get.call_args
        self.assertIn('/route/v1/driving/77.400000,23.200000;77.402000,23.200000', args[0])
        self.assertEqual(kwargs['params']['geometries'], 'geojson')
        self.assertEqual(kwargs['params']['steps'], 'true')
        self.assertEqual(kwargs['params']['alternatives'], 'true')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(result['routes'][0]['geometry']['coordinates'][1], [77.401, 23.201])
        self.assertEqual(result['routes'][0]['legs'][0]['steps'][0]['instruction'], 'Turn left onto Market Road')
        self.assertFalse(result['traffic_aware'])

    def test_full_trip_preserves_waypoint_order(self):
        self.get.return_value = response(road_payload(2))
        self.router.route(self.points + [[77.41, 23.22]])
        args, kwargs = self.get.call_args
        self.assertTrue(args[0].endswith('77.400000,23.200000;77.402000,23.200000;77.410000,23.220000'))
        self.assertEqual(kwargs['params']['alternatives'], 'false')

    def test_cache_is_bounded_fresh_and_cannot_be_mutated_by_callers(self):
        result = self.router.route(self.points)
        result['routes'].clear()
        self.assertTrue(self.router.route(self.points)['routes'])
        self.get.assert_called_once()
        self.now += 61
        self.router.route(self.points)
        self.assertEqual(self.get.call_count, 2)
        for i in range(130):
            self.now += 2
            self.router.route([[77.4, 23.2], [77.5, 23 + i / 1000]])
        self.assertLessEqual(len(self.router.cache), 128)

    def test_provider_rate_limit_prevents_extra_calls(self):
        self.router.route(self.points)
        with self.assertRaises(DirectionsError) as caught:
            self.router.route([[77.4, 23.2], [77.6, 23.3]])
        self.assertEqual(caught.exception.status, 429)
        self.get.assert_called_once()

    def test_missing_road_never_returns_a_straight_line(self):
        for code in ('NoRoute', 'NoSegment'):
            with self.subTest(code=code):
                self.now += 2
                self.get.return_value = response({'code': code}, 400)
                with self.assertRaises(DirectionsError) as caught:
                    self.router.route(self.points)
                self.assertEqual(caught.exception.code, 'NO_ROAD_ROUTE')
                self.assertEqual(caught.exception.status, 422)

    def test_invalid_and_incomplete_provider_responses_fail_closed(self):
        bad = [[], {'code': 'Ok', 'routes': []}]
        for mutation in ('geometry', 'duration', 'legs', 'steps'):
            item = deepcopy(road_payload())
            route = item['routes'][0]
            if mutation == 'geometry': route['geometry']['coordinates'] = [[900, 0], [77, 23]]
            if mutation == 'duration': route['duration'] = float('nan')
            if mutation == 'legs': route['legs'] = []
            if mutation == 'steps': route['legs'][0]['steps'] = []
            bad.append(item)
        for payload in bad:
            self.now += 2
            self.get.return_value = response(payload)
            with self.assertRaises(DirectionsError): self.router.route(self.points)

    def test_timeout_does_not_expose_coordinate_url(self):
        self.get.side_effect = requests.Timeout('private-coordinate-url')
        with self.assertRaises(DirectionsError) as caught: self.router.route(self.points)
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn('private-coordinate-url', str(caught.exception))

    def test_invalid_coordinates_and_excess_stops_do_not_contact_provider(self):
        for points in ([[float('nan'), 23], [77, 23]], [[77, 100], [77, 23]], [[77, 23]] * 26):
            with self.assertRaises(DirectionsError): self.router.route(points)
        self.get.assert_not_called()

    def test_roundabout_and_arrival_instructions(self):
        self.assertEqual(instruction({'maneuver': {'type': 'roundabout', 'exit': 2}, 'name': 'Farm Road'}),
                         'At the roundabout, take exit 2 onto Farm Road')
        self.assertEqual(instruction({'maneuver': {'type': 'arrive', 'modifier': 'left'}}), 'Arrive at this stop on the left')


if __name__ == '__main__': unittest.main()
