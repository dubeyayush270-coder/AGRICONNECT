"""Buyer catalog, checkout and order views using the shared transaction context."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
import math
import secrets

from flask import abort, make_response, redirect, render_template, request, session, url_for
import mysql.connector
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


MONEY = Decimal("0.01")
ACTIVE_STATUSES = ("PENDING_LOGISTICS", "LOGISTICS_ASSIGNED", "PICKED_UP", "IN_TRANSIT")


def positive_decimal(value, label):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"Enter a valid {label}.") from None
    if not number.is_finite() or number <= 0 or number > Decimal("99999999.99"):
        raise ValueError(f"Enter a valid {label}.")
    if number != number.quantize(MONEY):
        raise ValueError(f"Use at most two decimal places for {label}.")
    return number


def coordinates(latitude, longitude):
    try:
        lat, lng = float(latitude), float(longitude)
    except (ValueError, TypeError):
        raise ValueError("Select a valid delivery location on the map.") from None
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("Select a valid delivery location on the map.")
    return lat, lng


def estimate(product, quantity, latitude, longitude, rate):
    delivery = coordinates(latitude, longitude)
    try:
        pickup = coordinates(product.get("pickup_latitude"), product.get("pickup_longitude"))
    except ValueError:
        raise ValueError("The supplier must add a valid pickup location before this product can be ordered.") from None
    lat1, lat2 = math.radians(pickup[0]), math.radians(delivery[0])
    dlat, dlng = lat2 - lat1, math.radians(delivery[1] - pickup[1])
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    distance = Decimal(str(6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, a)))))).quantize(MONEY)
    price = positive_decimal(product["price_per_kg"], "product price")
    subtotal = (quantity * price).quantize(MONEY, rounding=ROUND_HALF_UP)
    cost = (distance * rate).quantize(MONEY, rounding=ROUND_HALF_UP)
    if subtotal + cost > Decimal("99999999.99"):
        raise ValueError("This order exceeds the supported amount. Please choose a smaller quantity.")
    return dict(distance_km=distance, product_price=price, product_total=subtotal,
                estimated_logistics_cost=cost, total_amount=subtotal + cost)


def register_buyer_routes(app, transaction, location_state):
    app.config.setdefault("DELIVERY_RATE_PER_KM", Decimal("15.00"))

    def buyer_only(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                return redirect("/")
            try:
                with transaction() as cursor:
                    # The same user lock serializes a buyer's checkout writes.
                    lock = " FOR UPDATE" if request.method == "POST" else ""
                    cursor.execute("SELECT id, name, role, district, state FROM users WHERE id = %s" + lock,
                                   (session["user_id"],))
                    user = cursor.fetchone()
                    if not user:
                        session.clear()
                        return redirect("/")
                    if str(user["role"]).lower() != "buyer":
                        abort(403)
                    result = view(cursor, user, *args, **kwargs)
                response = make_response(result)
            except mysql.connector.Error:
                app.logger.exception("Buyer database request failed")
                response = make_response(render_template("buyer-error.html",
                    message="Unable to load or save your order. Please try again.",
                    back_url=url_for("buyer")), 503)
            response.headers["Cache-Control"] = "no-store"
            return response
        return wrapped

    def csrf_token():
        if "buyer_csrf" not in session:
            session["buyer_csrf"] = secrets.token_urlsafe(32)
        return session["buyer_csrf"]

    def quote_signer():
        return URLSafeTimedSerializer(app.secret_key, salt="buyer-checkout-quote")

    def quote_payload(user, product_id, quantity, lat, lng, quote):
        return [user["id"], product_id, str(quantity.normalize()), float(lat), float(lng),
                str(quote["product_price"]), str(quote["total_amount"])]

    def product_for_order(cursor, product_id, lock=False):
        cursor.execute("SELECT * FROM products WHERE product_id = %s" + (" FOR UPDATE" if lock else ""),
                       (product_id,))
        product = cursor.fetchone()
        if not product:
            abort(404)
        return product

    def recent_orders(cursor, buyer_id, limit=10, offset=0):
        cursor.execute("""
            SELECT o.*, p.crop_name, farmer.name AS farmer_name,
                   driver.name AS driver_name, driver.phone AS driver_phone,
                   lp.vehicle_number, lp.vehicle_type
            FROM orders o
            LEFT JOIN products p ON p.product_id = o.product_id
            LEFT JOIN users farmer ON farmer.id = o.farmer_id
            LEFT JOIN logistics_profiles lp ON lp.logistics_id = o.assigned_logistics_id
            LEFT JOIN users driver ON driver.id = lp.user_id
            WHERE o.buyer_id = %s ORDER BY o.order_id DESC LIMIT %s OFFSET %s
        """, (buyer_id, limit, offset))
        return cursor.fetchall()

    @app.route("/buyer")
    @buyer_only
    def buyer(cursor, user):
        cursor.execute("""
            SELECT COUNT(*) AS total_orders,
                COALESCE(SUM(status NOT IN ('DELIVERED', 'CANCELLED')), 0) AS pending_orders,
                COALESCE(SUM(CASE WHEN status = 'DELIVERED' THEN total_amount ELSE 0 END), 0) AS spending,
                COALESCE(SUM(CASE WHEN status <> 'CANCELLED' THEN total_amount ELSE 0 END), 0) AS order_value,
                COALESCE(SUM(CASE WHEN status <> 'CANCELLED' THEN product_total ELSE 0 END), 0) AS product_value,
                COALESCE(SUM(CASE WHEN status <> 'CANCELLED' THEN estimated_logistics_cost ELSE 0 END), 0) AS delivery_value,
                COUNT(DISTINCT CASE WHEN status NOT IN ('DELIVERED', 'CANCELLED') THEN farmer_id END) AS active_suppliers
            FROM orders WHERE buyer_id = %s
        """, (user["id"],))
        stats = cursor.fetchone()
        orders = recent_orders(cursor, user["id"], 5)
        cursor.execute("""
            SELECT p.*, u.name AS supplier_name FROM products p JOIN users u ON u.id = p.user_id
            WHERE p.quantity > 0 AND p.user_id <> %s
            ORDER BY (p.district = %s) DESC, p.price_per_kg ASC, p.product_id DESC LIMIT 4
        """, (user["id"], user.get("district") or ""))
        products = cursor.fetchall()
        cursor.execute("""
            SELECT u.id, u.name, u.district, u.state, COUNT(*) AS listing_count
            FROM users u JOIN products p ON p.user_id = u.id
            WHERE p.quantity > 0 AND u.id <> %s
            GROUP BY u.id, u.name, u.district, u.state
            ORDER BY (u.district = %s) DESC, listing_count DESC, u.id LIMIT 4
        """, (user["id"], user.get("district") or ""))
        suppliers = cursor.fetchall()
        cursor.execute("""
            SELECT crop_name, MIN(price_per_kg) AS low_price, MAX(price_per_kg) AS high_price
            FROM products WHERE quantity > 0 GROUP BY crop_name ORDER BY crop_name
        """)
        return render_template("buyer-dashboard.html", user=user, stats=stats, orders=orders,
                               products=products, suppliers=suppliers, prices=cursor.fetchall(),
                               delivery_rate=app.config["DELIVERY_RATE_PER_KM"])

    @app.route("/browse-products")
    @buyer_only
    def browse_products(cursor, user):
        search = request.args.get("q", "").strip()[:100]
        supplier_id = request.args.get("supplier", type=int)
        page = max(1, request.args.get("page", 1, type=int))
        query = """SELECT p.*, u.name AS supplier_name FROM products p
                   JOIN users u ON u.id = p.user_id WHERE p.quantity > 0 AND p.user_id <> %s"""
        params = [user["id"]]
        if search:
            query += " AND (p.crop_name LIKE %s OR u.name LIKE %s OR p.district LIKE %s)"
            params.extend(["%" + search + "%"] * 3)
        if supplier_id:
            query += " AND p.user_id = %s"
            params.append(supplier_id)
        query += " ORDER BY p.created_at DESC, p.product_id DESC LIMIT 21 OFFSET %s"
        params.append((page - 1) * 20)
        cursor.execute(query, tuple(params))
        products = cursor.fetchall()
        return render_template(
    "browse-products.html",
    user=user,
    products=products[:20],
    q=search,
    supplier_id=supplier_id,
    page=page,
    has_next=len(products) > 20
)
    @app.route("/buy-product/<int:product_id>")
    @buyer_only
    def buy_product(cursor, user, product_id):
        product = product_for_order(cursor, product_id)
        if product["quantity"] <= 0:
            abort(404)
        return render_template("buy-product.html", product=product, csrf_token=csrf_token(),
                               delivery_rate=app.config["DELIVERY_RATE_PER_KM"])

    @app.route("/buyer/quote/<int:product_id>", methods=["POST"])
    @buyer_only
    def buyer_quote(cursor, user, product_id):
        if not secrets.compare_digest(request.form.get("csrf_token", ""), csrf_token()):
            abort(400)
        try:
            product = product_for_order(cursor, product_id)
            quantity = positive_decimal(request.form.get("quantity"), "quantity")
            if quantity > product["quantity"]:
                raise ValueError("Requested quantity exceeds available stock.")
            quote = estimate(product, quantity, request.form.get("delivery_latitude"),
                             request.form.get("delivery_longitude"), app.config["DELIVERY_RATE_PER_KM"])
            payload = quote_payload(user, product_id, quantity, request.form.get("delivery_latitude"),
                                    request.form.get("delivery_longitude"), quote)
            return {**{key: str(value) for key, value in quote.items()},
                    "quote_token": quote_signer().dumps(payload)}
        except ValueError as exc:
            return {"error": str(exc)}, 400

    @app.route("/place-order/<int:product_id>", methods=["POST"])
    @buyer_only
    def place_order(cursor, user, product_id):
        if not secrets.compare_digest(request.form.get("csrf_token", ""), csrf_token()):
            abort(400)
        product = product_for_order(cursor, product_id, lock=True)
        try:
            if product["user_id"] == user["id"]:
                raise ValueError("You cannot order your own product.")
            quantity = positive_decimal(request.form.get("quantity"), "quantity")
            if quantity > product["quantity"]:
                raise ValueError("Requested quantity exceeds available stock. Please choose a smaller quantity.")
            delivery = {}
            for field, limit in (("house_no", 100), ("street", 255), ("village_city", 100),
                                 ("district", 100), ("state", 100), ("pincode", 6)):
                value = request.form.get("delivery_" + field, "").strip()
                if not value or len(value) > limit:
                    raise ValueError("Complete the delivery address with valid field lengths.")
                delivery[field] = value
            if len(delivery["pincode"]) != 6 or not all("0" <= c <= "9" for c in delivery["pincode"]):
                raise ValueError("Enter a six-digit delivery pincode.")
            lat, lng = coordinates(request.form.get("delivery_latitude"), request.form.get("delivery_longitude"))
            quote = estimate(product, quantity, lat, lng, app.config["DELIVERY_RATE_PER_KM"])
            try:
                reviewed = quote_signer().loads(request.form.get("quote_token", ""), max_age=600)
            except (BadSignature, SignatureExpired):
                raise ValueError("Calculate a fresh delivery estimate before placing your order.") from None
            if reviewed != quote_payload(user, product_id, quantity, lat, lng, quote):
                raise ValueError("The quantity, location or price changed. Recalculate and review the estimate.")
            if not product.get("pickup_address"):
                raise ValueError("The supplier must complete the pickup address before you can order.")
        except ValueError as exc:
            return render_template("buy-product.html", product=product, error=str(exc),
                                   csrf_token=csrf_token(), delivery_rate=app.config["DELIVERY_RATE_PER_KM"]), 400
        cursor.execute("""
            INSERT INTO orders (product_id, buyer_id, farmer_id, quantity, product_price, product_total,
                pickup_house_no, pickup_street, pickup_village_city, pickup_district, pickup_state,
                pickup_address, pickup_latitude, pickup_longitude,
                delivery_house_no, delivery_street, delivery_village_city, delivery_pincode,
                delivery_district, delivery_state, delivery_address, delivery_latitude, delivery_longitude,
                distance_km, estimated_logistics_cost, total_amount, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING_LOGISTICS')
        """, (product_id, user["id"], product["user_id"], quantity, quote["product_price"], quote["product_total"],
              *[product.get("pickup_" + field) for field in ("house_no", "street", "village_city", "district",
                                                             "state", "address", "latitude", "longitude")],
              delivery["house_no"], delivery["street"], delivery["village_city"], delivery["pincode"],
              delivery["district"], delivery["state"], ", ".join(delivery.values()), lat, lng,
              quote["distance_km"], quote["estimated_logistics_cost"], quote["total_amount"]))
        order_id = cursor.lastrowid
        cursor.execute("UPDATE products SET quantity = quantity - %s WHERE product_id = %s", (quantity, product_id))
        # Invite vehicles with sufficient capacity, including offline drivers who can respond when online.
        cursor.execute("""
            INSERT INTO logistics_order_requests (order_id, logistics_id, status)
            SELECT %s, lp.logistics_id, 'PENDING' FROM logistics_profiles lp
            JOIN users u ON u.id = lp.user_id
            WHERE u.role = 'Logistics' AND lp.vehicle_capacity >= %s AND lp.vehicle_number <> ''
        """, (order_id, quantity))
        return redirect(url_for("buyer_order", order_id=order_id, placed=1), code=303)

    @app.route("/buyer/orders")
    @buyer_only
    def buyer_orders(cursor, user):
        page = max(1, request.args.get("page", 1, type=int))
        orders = recent_orders(cursor, user["id"], 21, (page - 1) * 20)
        return render_template("buyer-orders.html", user=user, orders=orders[:20],
                               page=page, has_next=len(orders) > 20)

    def owned_order(cursor, user, order_id):
        cursor.execute("""
            SELECT o.*, p.crop_name, farmer.name AS farmer_name, driver.name AS driver_name,
                   driver.phone AS driver_phone, lp.vehicle_number, lp.vehicle_type,
                   lp.availability, lp.current_latitude, lp.current_longitude, lp.logistics_id,
                   TIMESTAMPDIFF(SECOND, lp.location_updated_at, CURRENT_TIMESTAMP) AS location_age_seconds
            FROM orders o LEFT JOIN products p ON p.product_id = o.product_id
            LEFT JOIN users farmer ON farmer.id = o.farmer_id
            LEFT JOIN logistics_profiles lp ON lp.logistics_id = o.assigned_logistics_id
            LEFT JOIN users driver ON driver.id = lp.user_id
            WHERE o.order_id = %s AND o.buyer_id = %s
        """, (order_id, user["id"]))
        order = cursor.fetchone()
        if not order:
            abort(404)
        return order

    @app.route("/buyer/orders/<int:order_id>")
    @buyer_only
    def buyer_order(cursor, user, order_id):
        order = owned_order(cursor, user, order_id)
        return render_template("buyer-order.html", user=user, order=order,
                               tracking_allowed=order["status"] in ACTIVE_STATUSES and bool(order["assigned_logistics_id"]))

    @app.route("/buyer/orders/<int:order_id>/location")
    @buyer_only
    def buyer_order_location(cursor, user, order_id):
        order = owned_order(cursor, user, order_id)
        state = location_state(order if order["status"] in ACTIVE_STATUSES else None)
        state["order_status"] = order["status"]
        return state
