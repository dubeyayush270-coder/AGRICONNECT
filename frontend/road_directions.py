"""Bounded, cached OSRM driving directions. No straight-line fallback."""
from collections import OrderedDict
from copy import deepcopy
import math
import threading
import time
from urllib.parse import urlsplit

import requests


class DirectionsError(Exception):
    def __init__(self, message, code="ROUTING_UNAVAILABLE", status=503):
        super().__init__(message)
        self.code, self.status = code, status


def coordinate(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("Invalid map coordinate")
    lng, lat = (float(v) for v in value)
    if not (math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90):
        raise ValueError("Invalid map coordinate")
    return [lng, lat]


def positive_number(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("Invalid route distance or duration")
    return number


def instruction(step):
    maneuver = step["maneuver"]
    kind, modifier = maneuver.get("type", ""), maneuver.get("modifier", "")
    road = str(step.get("name") or step.get("ref") or "")[:200]
    onto = f" onto {road}" if road else ""
    modifiers = {"uturn": "make a U-turn", "sharp right": "turn sharply right",
                 "right": "turn right", "slight right": "bear right", "straight": "continue straight",
                 "slight left": "bear left", "left": "turn left", "sharp left": "turn sharply left"}
    turn = modifiers.get(modifier, "continue")
    if kind == "depart":
        return "Start driving" + (f" on {road}" if road else "")
    if kind == "arrive":
        return "Arrive at this stop" + (f" on the {modifier}" if modifier in ("left", "right") else "")
    if kind in ("roundabout", "rotary", "roundabout turn"):
        exit_number = maneuver.get("exit")
        return (f"At the roundabout, take exit {exit_number}" if isinstance(exit_number, int)
                else "Enter the roundabout") + onto
    if kind in ("exit roundabout", "exit rotary"):
        return "Exit the roundabout" + onto
    if kind == "merge":
        return "Merge" + (f" {modifier}" if modifier in ("left", "right") else "") + onto
    if kind in ("on ramp", "off ramp"):
        return "Take the ramp" + (f" to the {modifier}" if modifier in ("left", "right") else "") + onto
    if kind == "fork":
        return "Keep " + (modifier if modifier in ("left", "right", "straight") else "going") + onto
    return turn.capitalize() + onto


class RoadDirections:
    def __init__(self, base_url, *, http_get=None, clock=time.monotonic, cache_seconds=60, min_interval=1.0):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query
                or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("OSRM_BASE_URL must be an HTTP(S) server URL without credentials or query parameters.")
        self.base_url = base_url.rstrip("/")
        self.http_get = http_get or requests.get
        self.clock, self.cache_seconds, self.min_interval = clock, cache_seconds, min_interval
        self.cache, self.lock, self.next_request_at = OrderedDict(), threading.Lock(), 0.0

    def route(self, points):
        if not 2 <= len(points) <= 25:
            raise DirectionsError("Use next-stop directions for routes with more than 24 remaining stops.", "TOO_MANY_STOPS", 400)
        try:
            points = [coordinate(point) for point in points]
        except (ValueError, TypeError, OverflowError) as exc:
            raise DirectionsError("A route stop has an invalid location.", "INVALID_LOCATION", 409) from exc
        coordinates = ";".join(f"{lng:.6f},{lat:.6f}" for lng, lat in points)
        with self.lock:
            now = self.clock()
            cached = self.cache.get(coordinates)
            if cached and cached[0] > now:
                self.cache.move_to_end(coordinates)
                return deepcopy(cached[1])
            if now < self.next_request_at:
                raise DirectionsError("Directions are busy. Please try again in a moment.", "ROUTING_BUSY", 429)
            self.next_request_at = now + self.min_interval
        try:
            response = self.http_get(
                f"{self.base_url}/route/v1/driving/{coordinates}",
                params={"steps": "true", "geometries": "geojson", "overview": "full",
                        "alternatives": "true" if len(points) == 2 else "false",
                        "radiuses": ";".join(["250"] * len(points)), "continue_straight": "false"},
                headers={"User-Agent": "AgriConnect-RouteNavigation/1.0", "Accept": "application/json"},
                timeout=(3, 8), allow_redirects=False,
            )
            try:
                if response.status_code == 429:
                    raise DirectionsError("The directions service is busy. Try again shortly.", "ROUTING_BUSY", 429)
                if 300 <= response.status_code < 400:
                    raise DirectionsError("The configured directions service needs a direct endpoint.")
                payload = response.json()
                if payload.get("code") in ("NoRoute", "NoSegment"):
                    raise DirectionsError("No driving route reaches these locations. Check the pickup and delivery pins.", "NO_ROAD_ROUTE", 422)
                response.raise_for_status()
                if payload.get("code") != "Ok":
                    raise ValueError("Unexpected routing response")
                result = self._parse(payload, len(points) - 1)
            finally:
                response.close()
        except DirectionsError:
            raise
        except (requests.RequestException, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
            # Do not expose the upstream URL, which contains private coordinates.
            raise DirectionsError("Road directions are unavailable. Your saved stops are still shown; try again shortly.") from exc
        with self.lock:
            self.cache[coordinates] = (self.clock() + self.cache_seconds, result)
            self.cache.move_to_end(coordinates)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
        return deepcopy(result)

    @staticmethod
    def _parse(payload, leg_count):
        routes = []
        for route in payload["routes"][:3]:
            geometry = route["geometry"]
            if geometry["type"] != "LineString" or not 2 <= len(geometry["coordinates"]) <= 100000:
                raise ValueError("Missing road geometry")
            if len(route["legs"]) != leg_count:
                raise ValueError("Missing route legs")
            legs = []
            for leg in route["legs"]:
                steps = []
                if not leg["steps"] or len(leg["steps"]) > 2000:
                    raise ValueError("Missing maneuver steps")
                for step in leg["steps"]:
                    steps.append({"instruction": instruction(step), "distance_m": positive_number(step["distance"]),
                                  "duration_s": positive_number(step["duration"]),
                                  "location": coordinate(step["maneuver"]["location"]),
                                  "type": str(step["maneuver"]["type"]),
                                  "modifier": str(step["maneuver"].get("modifier", ""))})
                legs.append({"distance_m": positive_number(leg["distance"]),
                             "duration_s": positive_number(leg["duration"]), "steps": steps})
            routes.append({"distance_m": positive_number(route["distance"]),
                           "duration_s": positive_number(route["duration"]),
                           "geometry": {"type": "LineString", "coordinates": [coordinate(p) for p in geometry["coordinates"]]},
                           "legs": legs})
        if not routes:
            raise ValueError("No routes returned")
        return {"routes": routes, "provider": "OSRM", "travel_mode": "driving", "traffic_aware": False}
