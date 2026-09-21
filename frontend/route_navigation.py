"""Driver-owned route snapshots and road directions; saved stop order is preserved."""
from functools import wraps
import math
import os
import time

from flask import make_response, request, session
import mysql.connector

from delivery_routes import delivery_csrf_token
from road_directions import DirectionsError, RoadDirections


def register_route_navigation(app, transaction, get_profile, get_route, location_state):
    app.config.setdefault("OSRM_BASE_URL", os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org"))
    router = RoadDirections(app.config["OSRM_BASE_URL"])
    app.extensions["road_directions"] = router

    def load_snapshot():
        user_id = session.get("user_id")
        if not user_id:
            raise DirectionsError("Sign in to view your delivery route.", "LOGIN_REQUIRED", 401)
        with transaction() as cursor:
            cursor.execute("SELECT role FROM users WHERE id = %s", (user_id,))
            user = cursor.fetchone()
            if not user:
                session.clear()
                raise DirectionsError("Sign in to view your delivery route.", "LOGIN_REQUIRED", 401)
            if user["role"] != "Logistics":
                raise DirectionsError("Only the assigned driver can view this route.", "ACCESS_DENIED", 403)
            profile = get_profile(cursor, user_id)
            state = location_state(profile)
            snapshot = {"success": True, "route": None, "location_state": state,
                        "csrf_token": delivery_csrf_token()}
            if not profile:
                snapshot["message"] = "Add your vehicle and accept a delivery request to start a route."
                return snapshot
            route, stops = get_route(cursor, profile["logistics_id"])
            if not route:
                snapshot["message"] = "No active route. Accept a delivery request to see road directions."
                return snapshot
            cursor.execute("""SELECT order_id, status, assigned_logistics_id FROM orders
                WHERE logistics_route_id = %s ORDER BY order_id""", (route["route_id"],))
            orders = {row["order_id"]: row for row in cursor.fetchall()}
            if ({s["order_id"] for s in stops} != set(orders)
                    or any(o["assigned_logistics_id"] != profile["logistics_id"] for o in orders.values())):
                raise ValueError("The saved route assignments are inconsistent. Refresh your deliveries.")
            pending = []
            load, capacity = float(route["current_load_kg"]), float(profile["vehicle_capacity"])
            if not (math.isfinite(load) and math.isfinite(capacity) and 0 <= load <= capacity and capacity > 0):
                raise ValueError("The saved vehicle load or capacity is invalid.")
            peak = load
            for stop in stops:
                status = orders[stop["order_id"]]["status"]
                expected = ("PLANNED" if status == "LOGISTICS_ASSIGNED" or
                            stop["stop_type"] == "DELIVERY" and status in ("PICKED_UP", "IN_TRANSIT") else "COMPLETED")
                if status not in ("LOGISTICS_ASSIGNED", "PICKED_UP", "IN_TRANSIT", "DELIVERED") or stop["status"] != expected:
                    raise ValueError("The saved delivery status and route stops are inconsistent.")
                if stop["status"] != "PLANNED":
                    continue
                load += float(stop["quantity_delta"])
                peak = max(peak, load)
                if not math.isfinite(load) or load < -1e-8 or load > capacity + 1e-8:
                    raise ValueError("The saved route exceeds vehicle capacity.")
                pending.append({"stop_id": stop["stop_id"], "order_id": stop["order_id"],
                                "sequence_no": stop["sequence_no"], "stop_type": stop["stop_type"],
                                "latitude": float(stop["latitude"]), "longitude": float(stop["longitude"]),
                                "address": stop.get("address") or "Saved map location",
                                "quantity_kg": abs(float(stop["quantity_delta"])), "load_after_kg": round(load, 2),
                                "order_status": status,
                                "next_status": "PICKED_UP" if stop["stop_type"] == "PICKUP" else
                                               "IN_TRANSIT" if status == "PICKED_UP" else "DELIVERED"})
            snapshot["route"] = {"route_id": route["route_id"], "route_version": route["route_version"],
                                 "stops": pending, "completed_stops": len(stops) - len(pending),
                                 "current_load_kg": float(route["current_load_kg"]),
                                 "peak_load_kg": peak, "vehicle_capacity_kg": capacity}
            return snapshot

    def endpoint(view):
        @wraps(view)
        def wrapped():
            try:
                result = view()
            except DirectionsError as exc:
                result = ({"success": False, "message": str(exc), "code": exc.code}, exc.status)
            except mysql.connector.Error:
                app.logger.error("Unable to read driver route data")
                result = ({"success": False, "message": "Unable to load your route. Please try again.", "code": "DATABASE_UNAVAILABLE"}, 503)
            except (ValueError, TypeError, KeyError, OverflowError):
                app.logger.error("Invalid saved driver route data")
                result = ({"success": False, "message": "Your route data needs attention. Refresh your deliveries or contact support.", "code": "INVALID_ROUTE"}, 409)
            response = make_response(result)
            response.headers["Cache-Control"] = "no-store"
            if response.status_code == 429:
                response.headers["Retry-After"] = "5"
            return response
        return wrapped

    @app.get("/logistics/route")
    @endpoint
    def logistics_route():
        return load_snapshot()

    @app.get("/logistics/route/directions")
    @endpoint
    def logistics_route_directions():
        snapshot = load_snapshot()
        route = snapshot["route"]
        if not route:
            raise DirectionsError("No active route. Accept a delivery request first.", "NO_ACTIVE_ROUTE", 404)
        if (request.args.get("route_id") != str(route["route_id"])
                or request.args.get("route_version") != str(route["route_version"])):
            raise DirectionsError("Your stops have changed. Refresh the route before continuing.", "ROUTE_CHANGED", 409)
        scope = request.args.get("scope", "next")
        if scope not in ("next", "all"):
            raise DirectionsError("Choose the next stop or the full route.", "INVALID_SCOPE", 400)
        state = snapshot["location_state"]
        age = state.get("last_seen_seconds")
        if not state["location"] or age is None or age > state["stale_after_seconds"]:
            raise DirectionsError("Update your current location to get directions from the vehicle.", "LOCATION_STALE", 409)
        destination_stops = route["stops"][:1] if scope == "next" else route["stops"]
        origin = state["location"]
        points = [[origin["longitude"], origin["latitude"]]] + [[s["longitude"], s["latitude"]] for s in destination_stops]
        # The database connection has closed before contacting the road service.
        result = router.route(points)
        latest = load_snapshot()["route"]
        if not latest or (latest["route_id"], latest["route_version"]) != (route["route_id"], route["route_version"]):
            raise DirectionsError("Your stops changed while directions loaded. Refresh the route.", "ROUTE_CHANGED", 409)
        return {"success": True, **result, "route_id": route["route_id"], "route_version": route["route_version"],
                "scope": scope, "origin": origin, "origin_age_seconds": age,
                "stop_ids": [s["stop_id"] for s in destination_stops], "generated_at": int(time.time())}
