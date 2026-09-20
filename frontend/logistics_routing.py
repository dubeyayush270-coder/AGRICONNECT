from __future__ import annotations
from dataclasses import dataclass
import math
import os
from typing import Iterable, Mapping, Sequence

EARTH_RADIUS_KM = 6371.0088

@dataclass(frozen=True)
class RoutingPolicy:
    pickup_corridor_km: float = 2.0
    max_added_distance_km: float = 5.0
    max_added_distance_pct: float = 15.0
    hard_max_added_distance_km: float = 10.0

    @classmethod
    def from_env(cls):
        return cls(
            pickup_corridor_km=float(os.getenv("LOGISTICS_PICKUP_CORRIDOR_KM", "2.0")),
            max_added_distance_km=float(os.getenv("LOGISTICS_MAX_ADDED_DISTANCE_KM", "5.0")),
            max_added_distance_pct=float(os.getenv("LOGISTICS_MAX_ADDED_DISTANCE_PCT", "15.0")),
            hard_max_added_distance_km=float(os.getenv("LOGISTICS_HARD_MAX_ADDED_DISTANCE_KM", "10.0")),
        )

def _point(lat, lng):
    lat_f, lng_f = float(lat), float(lng)
    if not (math.isfinite(lat_f) and math.isfinite(lng_f) and -90 <= lat_f <= 90 and -180 <= lng_f <= 180):
        raise ValueError("Invalid latitude/longitude")
    return lat_f, lng_f

def haversine_km(a, b):
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    dlat, dlng = lat2-lat1, lng2-lng1
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlng/2)**2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))

def route_distance_km(points):
    return sum(haversine_km(a, b) for a, b in zip(points, points[1:]))

def _to_local_xy_km(point, origin):
    lat, lng = map(math.radians, point)
    lat0, lng0 = map(math.radians, origin)
    return (
        (lng-lng0) * math.cos((lat+lat0)/2) * EARTH_RADIUS_KM,
        (lat-lat0) * EARTH_RADIUS_KM,
    )

def point_to_segment_km(point, a, b):
    if a == b:
        return haversine_km(point, a)
    px, py = _to_local_xy_km(point, a)
    bx, by = _to_local_xy_km(b, a)
    denom = bx*bx + by*by
    if denom <= 1e-12:
        return math.hypot(px, py)
    t = max(0.0, min(1.0, (px*bx + py*by) / denom))
    return math.hypot(px - t*bx, py - t*by)

def distance_to_polyline_km(point, polyline):
    if not polyline:
        return math.inf
    if len(polyline) == 1:
        return haversine_km(point, polyline[0])
    return min(point_to_segment_km(point, a, b) for a, b in zip(polyline, polyline[1:]))

def _normalize_existing_stop(stop):
    return {
        "kind": "EXISTING",
        "stop_id": stop.get("stop_id"),
        "order_id": int(stop["order_id"]),
        "stop_type": str(stop["stop_type"]).upper(),
        "latitude": float(stop["latitude"]),
        "longitude": float(stop["longitude"]),
        "address": stop.get("address") or "",
        "quantity_delta": float(stop["quantity_delta"]),
    }

def _capacity_profile(stops, current_load_kg, vehicle_capacity_kg):
    load, capacity = float(current_load_kg), float(vehicle_capacity_kg)
    peak = load
    if load < -1e-9 or load > capacity + 1e-9:
        return {"valid": False, "peak_load_kg": peak, "final_load_kg": load}
    for stop in stops:
        load += float(stop["quantity_delta"])
        peak = max(peak, load)
        if load < -1e-9 or load > capacity + 1e-9:
            return {"valid": False, "peak_load_kg": peak, "final_load_kg": load}
    return {"valid": True, "peak_load_kg": peak, "final_load_kg": load}

def initial_route_plan(*, start_latitude, start_longitude, order, vehicle_capacity_kg):
    quantity = float(order["quantity"])
    if quantity <= 0 or quantity > float(vehicle_capacity_kg):
        return {"compatible": False, "reason": "CAPACITY"}
    start = _point(start_latitude, start_longitude)
    pickup = _point(order["pickup_latitude"], order["pickup_longitude"])
    delivery = _point(order["delivery_latitude"], order["delivery_longitude"])
    stops = [
        {"kind":"NEW","stop_id":None,"order_id":int(order["order_id"]),"stop_type":"PICKUP",
         "latitude":pickup[0],"longitude":pickup[1],"address":order.get("pickup_address") or "",
         "quantity_delta":quantity},
        {"kind":"NEW","stop_id":None,"order_id":int(order["order_id"]),"stop_type":"DELIVERY",
         "latitude":delivery[0],"longitude":delivery[1],"address":order.get("delivery_address") or "",
         "quantity_delta":-quantity},
    ]
    cap = _capacity_profile(stops, 0.0, vehicle_capacity_kg)
    if not cap["valid"]:
        return {"compatible": False, "reason": "CAPACITY"}
    distance = route_distance_km([start] + [(s["latitude"], s["longitude"]) for s in stops])
    return {
        "compatible": True, "reason": "NEW_ROUTE", "fit_type": "NEW_ROUTE",
        "pickup_deviation_km": 0.0, "added_distance_km": distance, "added_distance_pct": 0.0,
        "planned_distance_km": distance, "peak_load_kg": cap["peak_load_kg"],
        "pickup_sequence": 1, "delivery_sequence": 2, "stops": stops,
    }

def best_insertion(*, start_latitude, start_longitude, existing_stops, order,
                   vehicle_capacity_kg, current_load_kg=0.0, policy=None):
    policy = policy or RoutingPolicy.from_env()
    start = _point(start_latitude, start_longitude)
    existing = [_normalize_existing_stop(s) for s in existing_stops]
    quantity = float(order["quantity"])
    if quantity <= 0 or quantity > float(vehicle_capacity_kg):
        return {"compatible": False, "reason": "CAPACITY"}

    pickup = _point(order["pickup_latitude"], order["pickup_longitude"])
    delivery = _point(order["delivery_latitude"], order["delivery_longitude"])
    base_polyline = [start] + [(s["latitude"], s["longitude"]) for s in existing]
    pickup_deviation = distance_to_polyline_km(pickup, base_polyline)
    if pickup_deviation > policy.pickup_corridor_km:
        return {"compatible": False, "reason": "PICKUP_OUTSIDE_ROUTE_CORRIDOR",
                "pickup_deviation_km": pickup_deviation}

    base_distance = route_distance_km(base_polyline)
    pickup_stop = {"kind":"NEW","stop_id":None,"order_id":int(order["order_id"]),"stop_type":"PICKUP",
                   "latitude":pickup[0],"longitude":pickup[1],"address":order.get("pickup_address") or "",
                   "quantity_delta":quantity}
    delivery_stop = {"kind":"NEW","stop_id":None,"order_id":int(order["order_id"]),"stop_type":"DELIVERY",
                     "latitude":delivery[0],"longitude":delivery[1],"address":order.get("delivery_address") or "",
                     "quantity_delta":-quantity}

    best = None
    n = len(existing)
    for pickup_pos in range(n + 1):
        for delivery_pos in range(pickup_pos + 1, n + 2):
            candidate = [dict(s) for s in existing]
            candidate.insert(pickup_pos, dict(pickup_stop))
            candidate.insert(delivery_pos, dict(delivery_stop))
            cap = _capacity_profile(candidate, current_load_kg, vehicle_capacity_kg)
            if not cap["valid"]:
                continue
            coords = [start] + [(s["latitude"], s["longitude"]) for s in candidate]
            new_distance = route_distance_km(coords)
            added = max(0.0, new_distance - base_distance)
            if added > policy.hard_max_added_distance_km + 1e-9:
                continue
            added_pct = (added/base_distance*100.0) if base_distance > 1e-9 else (0.0 if added <= 1e-9 else math.inf)
            if not (added <= policy.max_added_distance_km + 1e-9 or
                    added_pct <= policy.max_added_distance_pct + 1e-9):
                continue
            result = {
                "compatible": True, "reason": "COMPATIBLE",
                "fit_type": "ROUTE_EXTENSION" if delivery_pos + 1 == len(candidate) else "ON_ROUTE",
                "pickup_deviation_km": pickup_deviation,
                "added_distance_km": added, "added_distance_pct": added_pct,
                "planned_distance_km": new_distance, "peak_load_kg": cap["peak_load_kg"],
                "pickup_sequence": pickup_pos + 1, "delivery_sequence": delivery_pos + 1,
                "stops": candidate,
            }
            if best is None or (result["added_distance_km"], result["planned_distance_km"]) < (best["added_distance_km"], best["planned_distance_km"]):
                best = result

    return best or {"compatible": False, "reason": "NO_FEASIBLE_INSERTION",
                    "pickup_deviation_km": pickup_deviation}
