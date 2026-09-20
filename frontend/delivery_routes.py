"""Driver delivery lists and the ordered delivery lifecycle."""
import secrets
from decimal import Decimal, InvalidOperation

from flask import request, session
from logistics_routing import route_distance_km


NEXT_STATUS = {
    "LOGISTICS_ASSIGNED": "PICKED_UP",
    "PICKED_UP": "IN_TRANSIT",
    "IN_TRANSIT": "DELIVERED",
}
STATUS_TIMESTAMP = {
    "PICKED_UP": "picked_up_at",
    "IN_TRANSIT": "in_transit_at",
    "DELIVERED": "delivered_at",
}


def delivery_csrf_token():
    if "delivery_csrf" not in session:
        session["delivery_csrf"] = secrets.token_urlsafe(32)
    return session["delivery_csrf"]


def delivery_list(cursor, logistics_id, history=False, limit=20, offset=0):
    statuses = ("DELIVERED", "CANCELLED") if history else tuple(NEXT_STATUS)
    placeholders = ", ".join(["%s"] * len(statuses))
    cursor.execute("""
        SELECT o.order_id, o.status, o.logistics_route_id, o.quantity, o.product_price, o.product_total,
               o.distance_km, o.estimated_logistics_cost, o.total_amount,
               o.pickup_address, o.pickup_latitude, o.pickup_longitude,
               o.delivery_address, o.delivery_latitude, o.delivery_longitude,
               UNIX_TIMESTAMP(o.logistics_assigned_at) AS logistics_assigned_at,
               UNIX_TIMESTAMP(o.picked_up_at) AS picked_up_at,
               UNIX_TIMESTAMP(o.in_transit_at) AS in_transit_at,
               UNIX_TIMESTAMP(o.delivered_at) AS delivered_at,
               p.crop_name, farmer.name AS farmer_name, farmer.phone AS farmer_phone,
               buyer.name AS buyer_name, buyer.phone AS buyer_phone
        FROM orders o
        LEFT JOIN products p ON p.product_id = o.product_id
        LEFT JOIN users farmer ON farmer.id = o.farmer_id
        LEFT JOIN users buyer ON buyer.id = o.buyer_id
        WHERE o.assigned_logistics_id = %s AND o.status IN (""" + placeholders + """ )
        ORDER BY o.order_id DESC LIMIT %s OFFSET %s
    """, (logistics_id, *statuses, limit, offset))
    records = cursor.fetchall()
    for record in records:
        record["next_status"] = NEXT_STATUS.get(record["status"])
    return records


def delivery_summary(cursor, logistics_id):
    cursor.execute("""
        SELECT COALESCE(SUM(status IN ('LOGISTICS_ASSIGNED','PICKED_UP','IN_TRANSIT')), 0) AS active_count,
               COALESCE(SUM(status = 'DELIVERED'), 0) AS completed_count,
               COALESCE(SUM(CASE WHEN status = 'DELIVERED' THEN estimated_logistics_cost ELSE 0 END), 0)
                   AS completed_delivery_estimate
        FROM orders WHERE assigned_logistics_id = %s
    """, (logistics_id,))
    return cursor.fetchone()


def register_delivery_routes(app, logistics_api, get_profile, get_route_context):
    @app.route("/logistics/deliveries")
    @logistics_api
    def logistics_deliveries(cursor, user_id):
        view = request.args.get("view", "active")
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            page = 0
        if view not in ("active", "history") or not 1 <= page <= 100000:
            return {"success": False, "message": "Choose active or history and a valid page number."}, 400
        profile = get_profile(cursor, user_id)
        records, summary = [], dict(active_count=0, completed_count=0, completed_delivery_estimate=0)
        if profile:
            records = delivery_list(cursor, profile["logistics_id"], view == "history", 21, (page - 1) * 20)
            summary = delivery_summary(cursor, profile["logistics_id"])
        return dict(success=True, deliveries=records[:20], summary=summary, page=page,
                    has_next=len(records) > 20, view=view, csrf_token=delivery_csrf_token())

    @app.route("/logistics/deliveries/<int:order_id>/status", methods=["POST"])
    @logistics_api
    def update_delivery_status(cursor, user_id, order_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return {"success": False, "message": "A JSON object is required."}, 400
        token = request.headers.get("X-CSRF-Token") or data.get("csrf_token")
        expected = session.get("delivery_csrf")
        if not isinstance(token, str) or not expected or not secrets.compare_digest(token, expected):
            return {"success": False, "message": "Refresh your delivery list before updating this order."}, 403
        target = data.get("status")
        if not isinstance(target, str) or target not in STATUS_TIMESTAMP:
            return {"success": False, "message": "Choose PICKED_UP, IN_TRANSIT or DELIVERED."}, 400
        # Same lock ordering as Accept: user (in the decorator), profile, then order.
        profile = get_profile(cursor, user_id, for_update=True)
        if not profile:
            return {"success": False, "message": "Delivery not found."}, 404
        cursor.execute("""
            SELECT order_id, status, quantity, logistics_route_id FROM orders
            WHERE order_id = %s AND assigned_logistics_id = %s FOR UPDATE
        """, (order_id, profile["logistics_id"]))
        order = cursor.fetchone()
        if not order:
            return {"success": False, "message": "Delivery not found."}, 404
        current = order["status"]
        if current == target:
            return dict(success=True, changed=False, order_id=order_id, status=current,
                        next_status=NEXT_STATUS.get(current), message="Delivery status already saved.")
        if NEXT_STATUS.get(current) != target:
            return dict(success=False, order_id=order_id, status=current,
                        next_status=NEXT_STATUS.get(current),
                        message="This delivery has changed or the requested step is out of order. Refresh the list."), 409

        # Legacy orders without a route keep their existing lifecycle. New
        # acceptances always assign a route and cannot use this compatibility path.
        route_id = order["logistics_route_id"]
        if route_id is not None:
            try:
                route, stops = get_route_context(cursor, profile["logistics_id"], for_update=True)
                if not route or route["route_id"] != route_id:
                    raise ValueError("This delivery does not belong to your active route.")
                order_stops = {s["stop_type"]: s for s in stops if s["order_id"] == order_id}
                pickup, delivery = order_stops.get("PICKUP"), order_stops.get("DELIVERY")
                quantity = Decimal(str(order["quantity"]))
                if (not quantity.is_finite() or quantity <= 0 or not pickup or not delivery
                        or Decimal(str(pickup["quantity_delta"])) != quantity
                        or Decimal(str(delivery["quantity_delta"])) != -quantity):
                    raise ValueError("The delivery quantity and route stops are inconsistent.")
                pickup_status = "PLANNED" if current == "LOGISTICS_ASSIGNED" else "COMPLETED"
                if pickup["status"] != pickup_status or delivery["status"] != "PLANNED":
                    raise ValueError("The delivery status and route stops are inconsistent.")
                # IN_TRANSIT consumes no stop and changes no load. It may be
                # recorded after pickup even when another order's stop is next.
                if target in ("PICKED_UP", "DELIVERED"):
                    planned = [s for s in stops if s["status"] == "PLANNED"]
                    expected_stop = pickup if target == "PICKED_UP" else delivery
                    if not planned or planned[0]["stop_id"] != expected_stop["stop_id"]:
                        next_stop = planned[0] if planned else None
                        message = ("Complete the next route stop first: "
                                   f"{next_stop['stop_type']} for order #{next_stop['order_id']}."
                                   if next_stop else "This route has no remaining planned stop.")
                        return dict(success=False, order_id=order_id, status=current,
                                    next_status=NEXT_STATUS.get(current), message=message), 409
                    load = Decimal(str(route["current_load_kg"])) + Decimal(str(expected_stop["quantity_delta"]))
                    capacity = Decimal(str(profile["vehicle_capacity"]))
                    if not capacity.is_finite() or load < 0 or load > capacity:
                        raise ValueError("This stop would exceed vehicle capacity or create a negative load.")
                    remaining = planned[1:]
                    if not remaining and load != 0:
                        raise ValueError("The final route stop must leave an empty vehicle.")
                    points = [(s["latitude"], s["longitude"]) for s in [expected_stop] + remaining]
                    distance = route_distance_km(points)
            except (ValueError, InvalidOperation, TypeError) as exc:
                return dict(success=False, order_id=order_id, status=current,
                            next_status=NEXT_STATUS.get(current), message=str(exc)), 409

            # All checks precede writes: a normal 409 response must never commit
            # a partial stop/load change through the transaction decorator.
            if target in ("PICKED_UP", "DELIVERED"):
                cursor.execute("""
                    UPDATE logistics_route_stops SET status = 'COMPLETED',
                        actual_arrival_at = COALESCE(actual_arrival_at, CURRENT_TIMESTAMP),
                        completed_at = CURRENT_TIMESTAMP
                    WHERE stop_id = %s AND route_id = %s AND status = 'PLANNED'
                """, (expected_stop["stop_id"], route_id))
                cursor.execute("""
                    UPDATE logistics_routes SET current_load_kg = %s,
                        planned_distance_km = %s, route_version = route_version + 1,
                        started_at = COALESCE(started_at, CURRENT_TIMESTAMP), status = %s,
                        completed_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE completed_at END
                    WHERE route_id = %s
                """, (load, distance, "ACTIVE" if remaining else "COMPLETED", not remaining, route_id))
        # Only the fixed allow-list above can supply a column name.
        timestamp = STATUS_TIMESTAMP[target]
        cursor.execute(f"""
            UPDATE orders SET status = %s, {timestamp} = CURRENT_TIMESTAMP
            WHERE order_id = %s AND assigned_logistics_id = %s
        """, (target, order_id, profile["logistics_id"]))
        return dict(success=True, changed=True, order_id=order_id, status=target,
                    next_status=NEXT_STATUS.get(target), message="Delivery status updated.")
