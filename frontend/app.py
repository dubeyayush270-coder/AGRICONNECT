from flask import Flask, render_template, request, session, redirect, make_response, url_for
from contextlib import contextmanager
from functools import wraps
import math
from decimal import Decimal, InvalidOperation
from logistics_routing import RoutingPolicy, best_insertion, initial_route_plan
import random
import smtplib
from email.message import EmailMessage
import mysql.connector
import requests

from forecast import forecast_demand
from locations import LOCATIONS

import os
from werkzeug.utils import secure_filename


app = Flask(__name__)

app.config["ANNASETU_ENDPOINTS"] = {
    "forgot_password": "forgot_password",
    "schemes": "government_schemes",
}

app.secret_key = os.getenv(
    "SECRET_KEY",
    "agrikconnect-local-development-key"
)

SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")

DATABASE_CONFIG = {
    "host": os.getenv("MYSQLHOST", "localhost"),
    "port": int(os.getenv("MYSQLPORT", "3306")),
    "user": os.getenv("MYSQLUSER", "root"),
    "password": os.getenv("MYSQLPASSWORD", ""),
    "database": os.getenv("MYSQLDATABASE", "agriconnect"),
}

db = mysql.connector.connect(**DATABASE_CONFIG)

UPLOAD_FOLDER = os.path.join(
    app.root_path,
    "static",
    "uploads"
)

os.makedirs(
    UPLOAD_FOLDER,
    exist_ok=True
)




@app.route("/", methods=["GET", "POST"])
def home():
    # Keep submissions from the previous login page working.
    if request.method == "POST":
        return login()
    return render_template("home.html")


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        login_id = request.form["login_id"]
        password = request.form["password"]

        cursor = db.cursor(dictionary=True)

        query = """
            SELECT *
            FROM users
            WHERE email = %s
            AND password = %s
        """

        cursor.execute(
            query,
            (login_id, password)
        )

        user = cursor.fetchone()

        cursor.close()

        if user:

            print("Login successful")

            session["user_id"] = user["id"]
            session["role"] = user["role"]

            if user["role"] == "farmer":

                return redirect("/seller")

            elif user["role"] == "buyer":

                return redirect("/buyer")

            elif user["role"] == "Logistics":

                return redirect("/logistics")

            elif user["role"] == "Admin":

                return redirect("/admin")

            else:

                return render_template(
                    "login.html",
                    message="Unknown user role"
                )

        else:

            return render_template(
                "login.html",
                message="Wrong email or password"
            )

    return render_template("login.html")


@app.route("/forgot-password")
def forgot_password():

    return render_template(
        "forgot-password.html"
    )


@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "GET":

        states = sorted(
            LOCATIONS.keys()
        )

        return render_template(
            "register.html",
            states=states,
            districts=[],
            markets=[],
            selected_state="",
            selected_district=""
        )

    step = request.form.get("step")

    if step == "state":

        name = request.form["name"]

        email = request.form["email"]

        phone = request.form["phone"]

        role = request.form["role"]

        state = request.form["state"]

        cursor = db.cursor()

        cursor.execute(
            """
            SELECT id
            FROM users
            WHERE email = %s
            """,
            (email,)
        )

        existing_user = cursor.fetchone()

        cursor.close()

        if existing_user:

            return render_template(
                "register.html",

                states=sorted(
                    LOCATIONS.keys()
                ),

                districts=[],

                markets=[],

                selected_state="",

                selected_district="",

                message="This email is already registered"
            )

        districts = sorted(
            LOCATIONS.get(
                state,
                {}
            ).keys()
        )

        session["registration_data"] = {

            "name": name,

            "email": email,

            "phone": phone,

            "role": role,

            "state": state
        }

        return render_template(
            "register.html",

            states=sorted(
                LOCATIONS.keys()
            ),

            districts=districts,

            markets=[],

            selected_state=state,

            selected_district=""
        )

    elif step == "district":

        registration_data = session.get(
            "registration_data"
        )

        if not registration_data:

            return redirect("/register")

        district = request.form["district"]

        state = registration_data["state"]

        markets = LOCATIONS.get(
            state,
            {}
        ).get(
            district,
            []
        )

        registration_data["district"] = district

        session["registration_data"] = registration_data

        return render_template(
            "register.html",

            states=sorted(
                LOCATIONS.keys()
            ),

            districts=sorted(
                LOCATIONS.get(
                    state,
                    {}
                ).keys()
            ),

            markets=markets,

            selected_state=state,

            selected_district=district
        )

    elif step == "register":

        registration_data = session.get(
            "registration_data"
        )

        if not registration_data:

            return redirect("/register")

        market = request.form["market"]

        password = request.form["password"]

        confirm_password = request.form[
            "confirm_password"
        ]

        if password != confirm_password:

            state = registration_data["state"]

            district = registration_data["district"]

            markets = LOCATIONS.get(
                state,
                {}
            ).get(
                district,
                []
            )

            return render_template(
                "register.html",

                states=sorted(
                    LOCATIONS.keys()
                ),

                districts=sorted(
                    LOCATIONS.get(
                        state,
                        {}
                    ).keys()
                ),

                markets=markets,

                selected_state=state,

                selected_district=district,

                message="Passwords do not match"
            )

        registration_data["market"] = market

        registration_data["password"] = password

        session["registration_data"] = registration_data

        otp = random.randint(
            100000,
            999999
        )

        session["otp"] = otp

        print(
            "\nGenerated OTP:",
            otp
        )

        msg = EmailMessage()

        msg["Subject"] = (
            "AgriConnect - Email Verification OTP"
        )

        msg["From"] = SENDER_EMAIL

        msg["To"] = registration_data["email"]

        msg.set_content(
            f"""
Hello {registration_data["name"]},

Your AgriConnect verification OTP is:

{otp}

Please do not share this OTP with anyone.

Thank you,
AgriConnect Team
"""
        )

        try:

            print("Sending OTP through Resend API...")

response = requests.post(
    "https://api.resend.com/emails",
    headers={
        "Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
        "Content-Type": "application/json",
    },
    json={
        "from": "AgriConnect <onboarding@resend.dev>",
        "to": [registration_data["email"]],
        "subject": "AgriConnect - Email Verification OTP",
        "html": f"""
            <h2>AgriConnect Email Verification</h2>

            <p>Hello {registration_data["name"]},</p>

            <p>Your AgriConnect verification OTP is:</p>

            <h1>{otp}</h1>

            <p>Please do not share this OTP with anyone.</p>

            <p>Thank you,<br>AgriConnect Team</p>
        """,
    },
    timeout=30,
)

print(
    "Resend response:",
    response.status_code,
    response.text
)

response.raise_for_status()

print("OTP sent successfully")

            return render_template(
                "verify-otp.html",
                message="OTP sent successfully. Please check your email."
            )

        except smtplib.SMTPAuthenticationError as e:

            print(
                "\nGmail Authentication Error:"
            )

            print(e)

            state = registration_data["state"]

            district = registration_data["district"]

            return render_template(
                "register.html",

                states=sorted(
                    LOCATIONS.keys()
                ),

                districts=sorted(
                    LOCATIONS.get(
                        state,
                        {}
                    ).keys()
                ),

                markets=LOCATIONS.get(
                    state,
                    {}
                ).get(
                    district,
                    []
                ),

                selected_state=state,

                selected_district=district,

                message=(
                    "Gmail login failed. "
                    "Please check your Gmail App Password."
                )
            )

        except Exception as e:

            print(
                "\nEmail Error:"
            )

            print(
                repr(e)
            )

            state = registration_data["state"]

            district = registration_data["district"]

            return render_template(
                "register.html",

                states=sorted(
                    LOCATIONS.keys()
                ),

                districts=sorted(
                    LOCATIONS.get(
                        state,
                        {}
                    ).keys()
                ),

                markets=LOCATIONS.get(
                    state,
                    {}
                ).get(
                    district,
                    []
                ),

                selected_state=state,

                selected_district=district,

                message=(
                    "Unable to send OTP. "
                    "Check terminal for error."
                )
            )

    return redirect("/register")




@app.route(
    "/verify-otp",
    methods=["GET", "POST"]
)
def verify_otp():

    if request.method == "POST":

        entered_otp = request.form["otp"]

        saved_otp = session.get("otp")

        if not saved_otp:

            return render_template(
                "verify-otp.html",

                message=(
                    "OTP expired. "
                    "Please register again."
                )
            )

        if entered_otp == str(saved_otp):

            registration_data = session.get(
                "registration_data"
            )

            if not registration_data:

                return redirect("/register")

            cursor = db.cursor()

            query = """
                INSERT INTO users
                (
                    name,
                    email,
                    phone,
                    role,
                    state,
                    district,
                    market,
                    password
                )
                VALUES
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
            """

            values = (

                registration_data["name"],

                registration_data["email"],

                registration_data["phone"],

                registration_data["role"],

                registration_data["state"],

                registration_data["district"],

                registration_data["market"],

                registration_data["password"]
            )

            try:

                cursor.execute(
                    query,
                    values
                )

                db.commit()

                print(
                    "User registered successfully"
                )

            except mysql.connector.Error as e:

                db.rollback()

                print(
                    "Database Error:",
                    e
                )

                cursor.close()

                return render_template(
                    "verify-otp.html",
                    message=(
                        "Registration failed. "
                        "Email or phone may already exist."
                    )
                )

            cursor.close()

            session.pop(
                "otp",
                None
            )

            session.pop(
                "registration_data",
                None
            )

            return redirect(url_for("login"))

        else:

            return render_template(
                "verify-otp.html",

                message="Wrong OTP"
            )

    return render_template(
        "verify-otp.html"
    )


@app.route("/seller")
def seller():

    user_id = session.get("user_id")

    if not user_id:

        return redirect(url_for("login"))

    cursor = db.cursor(
        dictionary=True
    )

    cursor.execute(
        """
        SELECT *
        FROM users
        WHERE id = %s
        """,
        (user_id,)
    )

    user = cursor.fetchone()

    cursor.close()

    if not user:

        session.clear()

        return redirect(url_for("login"))

    return render_template(
        "seller-dashboard.html",
        user=user
    )

@app.route("/seller/orders")
def seller_orders():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    if session.get("role") != "farmer":
        return redirect(url_for("login"))

    cursor = db.cursor(dictionary=True)

    try:

        # ==========================================
        # CURRENT SELLER
        # ==========================================

        cursor.execute(
            """
            SELECT
                id,
                name,
                email,
                phone,
                role,
                state,
                district,
                market
            FROM users
            WHERE id = %s
            """,
            (user_id,)
        )

        user = cursor.fetchone()

        if not user:
            session.clear()
            return redirect(url_for("login"))


        # ==========================================
        # ORDERS FOR THIS SELLER
        # ==========================================

        cursor.execute(
            """
            SELECT
                o.order_id,
                o.quantity,
                o.product_price,
                o.product_total,
                o.estimated_logistics_cost,
                o.total_amount,
                o.status,
                o.created_at,

                o.pickup_address,
                o.delivery_address,

                o.assigned_logistics_id,
                o.logistics_assigned_at,

                p.product_id,
                p.crop_name,
                p.product_image,

                buyer.id AS buyer_id,
                buyer.name AS buyer_name,
                buyer.phone AS buyer_phone,
                buyer.email AS buyer_email,

                driver.name AS logistics_name,
                driver.phone AS logistics_phone,

                lp.vehicle_number,
                lp.vehicle_type,
                lp.availability

            FROM orders o

            INNER JOIN products p
                ON p.product_id = o.product_id

            INNER JOIN users buyer
                ON buyer.id = o.buyer_id

            LEFT JOIN logistics_profiles lp
                ON lp.logistics_id = o.assigned_logistics_id

            LEFT JOIN users driver
                ON driver.id = lp.user_id

            WHERE o.farmer_id = %s

            ORDER BY o.order_id DESC
            """,
            (user_id,)
        )

        orders = cursor.fetchall()

    finally:

        cursor.close()


    return render_template(
        "seller-orders.html",
        user=user,
        orders=orders
    )


@app.route("/seller/orders/<int:order_id>")
def seller_order_details(order_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    if session.get("role") != "farmer":
        return redirect(url_for("login"))

    cursor = db.cursor(dictionary=True)

    try:

        cursor.execute(
            """
            SELECT
                o.*,

                p.crop_name,
                p.product_image,

                buyer.name AS buyer_name,
                buyer.email AS buyer_email,
                buyer.phone AS buyer_phone,

                driver.name AS logistics_name,
                driver.phone AS logistics_phone,

                lp.logistics_id,
                lp.vehicle_number,
                lp.vehicle_type,
                lp.vehicle_capacity,
                lp.availability,
                lp.current_latitude,
                lp.current_longitude

            FROM orders o

            INNER JOIN products p
                ON p.product_id = o.product_id

            INNER JOIN users buyer
                ON buyer.id = o.buyer_id

            LEFT JOIN logistics_profiles lp
                ON lp.logistics_id = o.assigned_logistics_id

            LEFT JOIN users driver
                ON driver.id = lp.user_id

            WHERE o.order_id = %s
              AND o.farmer_id = %s
            """,
            (order_id, user_id)
        )

        order = cursor.fetchone()

    finally:
        cursor.close()

    if not order:
        return "Order not found", 404

    return render_template(
        "seller-order-details.html",
        order=order
    )

@app.route("/my-products")
def my_products():

    user_id = session.get("user_id")

    if not user_id:

        return redirect(url_for("login"))

    cursor = db.cursor(
        dictionary=True
    )

    query = """
        SELECT *
        FROM products
        WHERE user_id = %s
        ORDER BY created_at DESC
    """

    cursor.execute(
        query,
        (user_id,)
    )

    products = cursor.fetchall()

    cursor.close()

    return render_template(
        "my-products.html",
        products=products
    )


@app.route("/add-product", methods=["GET", "POST"])
def add_product():

    # =====================================================
    # CHECK LOGIN
    # =====================================================

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))


    # =====================================================
    # GET REQUEST
    # =====================================================

    if request.method == "GET":

        return render_template(
            "add.html"
        )


    # =====================================================
    # POST REQUEST
    # =====================================================

    image_filename = None

    try:

        # =================================================
        # PRODUCT DETAILS
        # =================================================

        crop_name = request.form.get(
            "crop_name",
            ""
        ).strip()

        quantity = request.form.get(
            "quantity",
            ""
        ).strip()

        quality = request.form.get(
            "quality",
            ""
        ).strip()

        price_per_kg = request.form.get(
            "price_per_kg",
            ""
        ).strip()

        description = request.form.get(
            "description",
            ""
        ).strip()


        # =================================================
        # PICKUP ADDRESS
        # =================================================

        pickup_house_no = request.form.get(
            "pickup_house_no",
            ""
        ).strip()

        pickup_street = request.form.get(
            "pickup_street",
            ""
        ).strip()

        pickup_village_city = request.form.get(
            "pickup_village_city",
            ""
        ).strip()

        pickup_pincode = request.form.get(
            "pickup_pincode",
            ""
        ).strip()

        pickup_state = request.form.get(
            "pickup_state",
            ""
        ).strip()

        pickup_district = request.form.get(
            "pickup_district",
            ""
        ).strip()


        # =================================================
        # LATITUDE / LONGITUDE
        # =================================================

        pickup_latitude = request.form.get(
            "pickup_latitude",
            ""
        ).strip()

        pickup_longitude = request.form.get(
            "pickup_longitude",
            ""
        ).strip()


        # =================================================
        # BASIC VALIDATION
        # =================================================

        if not crop_name:

            return render_template(
                "add.html",
                message="Please enter crop name."
            )


        if not quantity:

            return render_template(
                "add.html",
                message="Please enter quantity."
            )


        if not quality:

            return render_template(
                "add.html",
                message="Please select quality."
            )


        if not price_per_kg:

            return render_template(
                "add.html",
                message="Please enter price per kg."
            )


        # =================================================
        # PICKUP ADDRESS VALIDATION
        # =================================================

        if not pickup_house_no:

            return render_template(
                "add.html",
                message="Please enter house number."
            )


        if not pickup_street:

            return render_template(
                "add.html",
                message="Please enter street/locality."
            )


        if not pickup_village_city:

            return render_template(
                "add.html",
                message="Please enter village/city."
            )


        if not pickup_pincode:

            return render_template(
                "add.html",
                message="Please enter pincode."
            )


        # Check pincode format

        if not pickup_pincode.isdigit() \
                or len(pickup_pincode) != 6:

            return render_template(
                "add.html",
                message="Please enter a valid 6-digit pincode."
            )


        if not pickup_state or not pickup_district:

            return render_template(
                "add.html",
                message=(
                    "Please enter a valid pincode "
                    "and wait for State and District."
                )
            )


        # =================================================
        # COORDINATE VALIDATION
        # =================================================

        if not pickup_latitude or not pickup_longitude:

            return render_template(
                "add.html",
                message=(
                    "Please select your pickup location "
                    "on the map."
                )
            )


        # Convert coordinates to float

        try:

            pickup_latitude = float(
                pickup_latitude
            )

            pickup_longitude = float(
                pickup_longitude
            )

        except ValueError:

            return render_template(
                "add.html",
                message=(
                    "Invalid pickup coordinates."
                )
            )


        # =================================================
        # CHECK COORDINATE RANGE
        # =================================================

        if not (
            -90 <= pickup_latitude <= 90
        ):

            return render_template(
                "add.html",
                message="Invalid latitude."
            )


        if not (
            -180 <= pickup_longitude <= 180
        ):

            return render_template(
                "add.html",
                message="Invalid longitude."
            )


        # =================================================
        # IMAGE UPLOAD
        # =================================================

        image = request.files.get(
            "product_image"
        )


        if image and image.filename:

            image_filename = secure_filename(
                image.filename
            )


            image_path = os.path.join(
                UPLOAD_FOLDER,
                image_filename
            )


            image.save(
                image_path
            )


        # =================================================
        # FULL PICKUP ADDRESS
        # =================================================

        pickup_address = (
            pickup_house_no
            + ", "
            + pickup_street
            + ", "
            + pickup_village_city
            + ", "
            + pickup_district
            + ", "
            + pickup_state
            + ", India - "
            + pickup_pincode
        )


        # =================================================
        # GET USER STATE / DISTRICT
        # =================================================

        cursor = db.cursor(
            dictionary=True
        )


        cursor.execute(
            """
            SELECT state, district
            FROM users
            WHERE id = %s
            """,
            (user_id,)
        )


        user = cursor.fetchone()


        if not user:

            cursor.close()

            return redirect(url_for("login"))


        # =================================================
        # INSERT PRODUCT
        # =================================================

        query = """
            INSERT INTO products
            (
                user_id,
                crop_name,
                quantity,
                quality,
                price_per_kg,
                state,
                district,
                product_image,
                description,

                pickup_house_no,
                pickup_street,
                pickup_village_city,
                pickup_district,
                pickup_state,
                pickup_address,
                pickup_pincode,

                pickup_latitude,
                pickup_longitude
            )

            VALUES
            (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,

                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,

                %s,
                %s
            )
        """


        values = (

            user_id,

            crop_name,

            quantity,

            quality,

            price_per_kg,

            user["state"],

            user["district"],

            image_filename,

            description,


            pickup_house_no,

            pickup_street,

            pickup_village_city,

            pickup_district,

            pickup_state,

            pickup_address,

            pickup_pincode,


            pickup_latitude,

            pickup_longitude
        )


        # =================================================
        # DATABASE INSERT
        # =================================================

        try:

            cursor.execute(
                query,
                values
            )


            db.commit()


            print(
                "================================="
            )

            print(
                "PRODUCT ADDED SUCCESSFULLY"
            )

            print(
                "Product:",
                crop_name
            )

            print(
                "Pickup Latitude:",
                pickup_latitude
            )

            print(
                "Pickup Longitude:",
                pickup_longitude
            )

            print(
                "================================="
            )


        except mysql.connector.Error as e:

            db.rollback()

            print(
                "DATABASE ERROR:",
                e
            )


            cursor.close()


            # Delete uploaded image
            # if database insertion fails

            if image_filename:

                image_path = os.path.join(
                    UPLOAD_FOLDER,
                    image_filename
                )


                if os.path.exists(
                    image_path
                ):

                    os.remove(
                        image_path
                    )


            return render_template(
                "add.html",
                message=(
                    "Product save nahi ho paaya. "
                    "Please try again."
                )
            )


        # =================================================
        # CLOSE CURSOR
        # =================================================

        cursor.close()


        # =================================================
        # SUCCESS
        # =================================================

        return redirect(
            "/my-products"
        )


    # =====================================================
    # OTHER ERROR
    # =====================================================

    except Exception as e:

        print(
            "ADD PRODUCT ERROR:",
            repr(e)
        )


        # Remove image if some unexpected
        # error happened after upload

        if image_filename:

            image_path = os.path.join(
                UPLOAD_FOLDER,
                image_filename
            )


            if os.path.exists(
                image_path
            ):

                try:

                    os.remove(
                        image_path
                    )

                except Exception:
                    pass


        return render_template(
            "add.html",
            message=(
                "Something went wrong. "
                "Please check your details."
            )
        )

    


@app.route("/delete-product")
def delete_product():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    cursor = db.cursor(
        dictionary=True
    )

    cursor.execute(
        """
        SELECT *
        FROM products
        WHERE user_id = %s
        ORDER BY created_at DESC
        """,
        (user_id,)
    )

    products = cursor.fetchall()

    cursor.close()

    return render_template(
        "delete-product.html",
        products=products
    )


@app.route(
    "/delete-product",
    methods=["POST"]
)
def delete_selected_product():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    product_id = request.form[
        "product_id"
    ]

    cursor = db.cursor(
        dictionary=True
    )

    # Pehle product ki image ka naam nikalo
    cursor.execute(
        """
        SELECT product_image
        FROM products
        WHERE product_id = %s
        AND user_id = %s
        """,
        (
            product_id,
            user_id
        )
    )

    product = cursor.fetchone()

    # Product ki complete row delete karo
    cursor.execute(
        """
        DELETE FROM products
        WHERE product_id = %s
        AND user_id = %s
        """,
        (
            product_id,
            user_id
        )
    )

    db.commit()

    cursor.close()

    # Uploaded image ko bhi delete karo
    if product and product["product_image"]:

        image_path = os.path.join(
            UPLOAD_FOLDER,
            product["product_image"]
        )

        if os.path.exists(image_path):
            os.remove(image_path)

    return redirect(
        "/my-products"
    )



@app.route("/demand-forecast")
def demand_forecast():

    user_id = session.get("user_id")

    if not user_id:

        return redirect(url_for("login"))

    cursor = db.cursor(
        dictionary=True
    )

    cursor.execute(
        """
        SELECT
            state,
            district,
            market
        FROM users
        WHERE id = %s
        """,
        (user_id,)
    )

    user = cursor.fetchone()

    cursor.close()

    if not user:

        session.clear()

        return redirect(url_for("login"))

    state = user["state"]

    district = user["district"]

    market = user["market"]

    print("\n====================================")
    print("DEMAND FORECAST")
    print("====================================")

    print(
        "State:",
        state
    )

    print(
        "District:",
        district
    )

    print(
        "Market:",
        market
    )

    if not market:

        return render_template(
            "demand-forecast.html",

            state=state,

            district=district,

            market="Not Available",

            forecast_results=[],

            highest_demand_crop=None,

            message=(
                "Market information is not available "
                "for your account."
            )
        )

    crops = [

        "Tomato",

        "Onion",

        "Potato",

        "Wheat",

        "Soybean"

    ]

    forecast_results = []

    for crop in crops:

        print(
            "\nForecasting:",
            crop
        )

        try:

            forecast_df = forecast_demand(
                market,
                crop
            )

            if forecast_df is not None:

                total_demand = forecast_df[
                    "predicted_demand"
                ].sum()

                forecast_results.append({

                    "crop": crop,

                    "total_demand": round(
                        float(total_demand),
                        2
                    ),

                    "daily_forecast":
                        forecast_df.to_dict(
                            "records"
                        )

                })

                print(
                    crop,
                    "=>",
                    round(
                        float(total_demand),
                        2
                    ),
                    "kg"
                )

            else:

                print(
                    "No forecast available for",
                    crop
                )

        except Exception as e:

            print(
                "Forecast Error:",
                crop,
                repr(e)
            )

    if forecast_results:

        highest_demand_crop = max(
            forecast_results,
            key=lambda x: x["total_demand"]
        )

        print(
            "\nHighest Demand Crop:",
            highest_demand_crop["crop"]
        )

    else:

        highest_demand_crop = None

        print(
            "\nNo forecast results available."
        )

    print(
        "====================================\n"
    )

    return render_template(
        "demand-forecast.html",

        state=state,

        district=district,

        market=market,

        forecast_results=forecast_results,

        highest_demand_crop=highest_demand_crop
    )


@app.route("/view/<int:product_id>")
def view_product(product_id):

    cursor = db.cursor(dictionary=True)

    query = """
        SELECT *
        FROM products
        WHERE product_id = %s
    """

    cursor.execute(query, (product_id,))
    product = cursor.fetchone()

    cursor.close()

    if not product:
        return "Product not found", 404

    return render_template(
        "view-product.html",
        product=product
    )



@app.route("/government-schemes")
def government_schemes():

    # Logged-in user ki ID
    user_id = session.get("user_id")

    # Login nahi hai
    if not user_id:
        return redirect(url_for("login"))

    cursor = db.cursor(dictionary=True)

    # User ki complete information fetch karo
    cursor.execute(
        """
        SELECT id, name, email, phone, role, state, district, market
        FROM users
        WHERE id = %s
        """,
        (user_id,)
    )

    user = cursor.fetchone()

    cursor.close()

    # User database me nahi mila
    if not user:
        session.clear()
        return redirect(url_for("login"))

    # User information scheme.html ko bhejo
    return render_template(
        "scheme.html",
        user=user
    )




LOCATION_STALE_SECONDS = 60
LOGISTICS_VEHICLE_TYPES = {"Mini Truck", "Pickup", "Tractor", "Small Truck", "Truck"}


@contextmanager
def logistics_cursor():
    """Give each logistics request its own connection and transaction."""
    connection = mysql.connector.connect(**DATABASE_CONFIG)
    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        try:
            if cursor is not None:
                cursor.close()
        finally:
            connection.close()


def logistics_api(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        if not user_id:
            result = ({"success": False, "message": "Login required"}, 401)
        else:
            try:
                with logistics_cursor() as cursor:
                    # Serialize writes for this driver, including profile creation.
                    lock = " FOR UPDATE" if request.method == "POST" else ""
                    cursor.execute("SELECT role FROM users WHERE id = %s" + lock, (user_id,))
                    user = cursor.fetchone()
                    if not user:
                        session.clear()
                        result = ({"success": False, "message": "Login required"}, 401)
                    elif user["role"] != "Logistics":
                        result = ({"success": False, "message": "Access denied"}, 403)
                    else:
                        result = view(cursor, user_id, *args, **kwargs)
            except mysql.connector.Error:
                app.logger.exception("Logistics database request failed")
                result = ({"success": False, "message": "Unable to complete this logistics request. Please try again."}, 503)
        response = make_response(result)
        response.headers["Cache-Control"] = "no-store"
        return response
    return wrapped


def get_logistics_profile(cursor, user_id, for_update=False):
    cursor.execute("""
        SELECT logistics_id, vehicle_number, vehicle_type, vehicle_capacity,
               availability, current_latitude, current_longitude,
               TIMESTAMPDIFF(SECOND, location_updated_at, CURRENT_TIMESTAMP)
                   AS location_age_seconds
        FROM logistics_profiles
        WHERE user_id = %s
    """ + (" FOR UPDATE" if for_update else ""), (user_id,))
    return cursor.fetchone()


def logistics_location_state(profile):
    """Use database time for freshness; no assumptions about its timezone."""
    profile = profile or {}
    latitude = profile.get("current_latitude")
    longitude = profile.get("current_longitude")
    location = None
    if latitude is not None and longitude is not None:
        latitude, longitude = float(latitude), float(longitude)
        if math.isfinite(latitude) and math.isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180:
            location = {"latitude": latitude, "longitude": longitude}
    age = profile.get("location_age_seconds")
    age = int(age) if age is not None and age >= 0 else None
    availability = profile.get("availability") or "OFFLINE"
    return {
        "success": True,
        "profile_exists": profile.get("logistics_id") is not None,
        "availability": availability,
        "location": location,
        "last_seen_seconds": age if location else None,
        "stale_after_seconds": LOCATION_STALE_SECONDS,
        "is_live": bool(location and availability == "ONLINE" and age is not None and age <= LOCATION_STALE_SECONDS),
    }


@app.route("/logistics")
def logistics_dashboard():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))
    try:
        with logistics_cursor() as cursor:
            cursor.execute("""
                SELECT id, name, email, phone, role, state, district, market
                FROM users WHERE id = %s
            """, (user_id,))
            user = cursor.fetchone()
            if not user:
                session.clear()
                return redirect(url_for("login"))
            if user["role"] != "Logistics":
                return "Access denied", 403
            profile = get_logistics_profile(cursor, user_id)
            active_records, history_records = [], []
            if profile:
                active_records = delivery_list(cursor, profile["logistics_id"])
                history_records = delivery_list(cursor, profile["logistics_id"], history=True)
    except mysql.connector.Error:
        app.logger.exception("Unable to load logistics dashboard")
        return "Unable to load the logistics dashboard. Please try again.", 503
    user.update(profile or dict.fromkeys((
        "logistics_id", "vehicle_number", "vehicle_type", "vehicle_capacity",
        "availability", "current_latitude", "current_longitude",
    )))
    response = make_response(render_template(
        "logistics-dashboard.html", user=user,
        location_state=logistics_location_state(profile),
        available_requests=[],
        active_deliveries=[delivery_label(row) for row in active_records],
        delivery_history=[delivery_label(row) for row in history_records],
        active_delivery_records=active_records, delivery_history_records=history_records,
        delivery_csrf_token=delivery_csrf_token(),
    ))
    response.headers["Cache-Control"] = "no-store"
    return response


def logistics_weight(value):
    """Reject invalid stored weights before passing them to the float planner."""
    try:
        value = Decimal(str(value))
        if not value.is_finite():
            raise ValueError("Invalid route weight.")
        return value
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid route weight.") from exc


def get_logistics_route(cursor, logistics_id, for_update=False):
    """Read a complete route snapshot; writes hold user/profile before this lock.

    Locking reads are required on acceptance/status updates so a transaction
    waiting for the driver lock sees the previous transaction's committed stops.
    Completed stops stay in place; only the planned suffix may be re-sequenced.
    """
    lock = " FOR UPDATE" if for_update else ""
    cursor.execute("""
        SELECT route_id, logistics_id, status, current_load_kg, route_version
        FROM logistics_routes WHERE logistics_id = %s AND status = 'ACTIVE'
        ORDER BY route_id
    """ + lock, (logistics_id,))
    routes = cursor.fetchall()
    if len(routes) > 1:
        raise ValueError("This driver has multiple active routes. Please contact support.")
    if not routes:
        return None, []
    route = routes[0]
    cursor.execute("""
        SELECT stop_id, route_id, order_id, stop_type, sequence_no,
               latitude, longitude, address, quantity_delta, status
        FROM logistics_route_stops WHERE route_id = %s
        ORDER BY sequence_no, stop_id
    """ + lock, (route["route_id"],))
    stops = cursor.fetchall()
    load = logistics_weight(route["current_load_kg"])
    completed_load = Decimal("0")
    remaining_load = load
    seen, sequences = {}, set()
    planned_seen = False
    for stop in stops:
        delta = logistics_weight(stop["quantity_delta"])
        latitude, longitude = float(stop["latitude"]), float(stop["longitude"])
        if (not math.isfinite(latitude) or not math.isfinite(longitude)
                or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
            raise ValueError("The active route contains an invalid location.")
        sequence = stop["sequence_no"]
        if sequence < 1 or sequence in sequences:
            raise ValueError("The active route has an invalid stop sequence.")
        sequences.add(sequence)
        order_id = stop["order_id"]
        if stop["stop_type"] == "PICKUP" and delta > 0 and order_id not in seen:
            seen[order_id] = delta
        elif stop["stop_type"] == "DELIVERY" and delta < 0 and seen.get(order_id) == -delta:
            seen[order_id] = None
        else:
            raise ValueError("The active route contains inconsistent order stops.")
        if stop["status"] == "COMPLETED" and not planned_seen:
            completed_load += delta
            if completed_load < 0:
                raise ValueError("The active route contains an invalid load history.")
        elif stop["status"] == "PLANNED":
            planned_seen = True
            remaining_load += delta
            if remaining_load < 0:
                raise ValueError("The active route contains an invalid remaining load.")
        else:
            raise ValueError("The active route contains an invalid stop status/order.")
    if (not planned_seen or load < 0 or completed_load != load or remaining_load != 0
            or any(value is not None for value in seen.values())):
        raise ValueError("The active route load or stops are inconsistent. Please contact support.")
    return route, stops


def has_unrouted_deliveries(cursor, logistics_id, for_update=False):
    # Pre-migration deliveries have no trustworthy stop/load history. Let the
    # existing lifecycle finish them before starting a new shared-load route.
    cursor.execute("""
        SELECT order_id FROM orders
        WHERE assigned_logistics_id = %s AND logistics_route_id IS NULL
          AND status IN ('LOGISTICS_ASSIGNED', 'PICKED_UP', 'IN_TRANSIT')
        ORDER BY order_id LIMIT 1
    """ + (" FOR UPDATE" if for_update else ""), (logistics_id,))
    return cursor.fetchone() is not None


def logistics_route_plan(profile, order, route, stops):
    """Evaluate an invitation against the remaining route, never cached fit data."""
    try:
        capacity = logistics_weight(profile["vehicle_capacity"])
        quantity = logistics_weight(order["quantity"])
        if capacity <= 0 or quantity <= 0 or quantity > capacity:
            return {"compatible": False, "reason": "CAPACITY"}
        location_state = logistics_location_state(profile)
        location = location_state["location"]
        completed = [stop for stop in stops if stop["status"] == "COMPLETED"]
        remaining = [stop for stop in stops if stop["status"] == "PLANNED"]
        if completed and not location_state["is_live"]:
            start = (completed[-1]["latitude"], completed[-1]["longitude"])
        elif location:
            start = (location["latitude"], location["longitude"])
        elif remaining:
            start = (remaining[0]["latitude"], remaining[0]["longitude"])
        else:
            start = (order["pickup_latitude"], order["pickup_longitude"])
        arguments = dict(start_latitude=start[0], start_longitude=start[1],
                         order=order, vehicle_capacity_kg=capacity)
        if route is None:
            return initial_route_plan(**arguments)
        return best_insertion(**arguments, existing_stops=remaining,
                              current_load_kg=route["current_load_kg"],
                              policy=RoutingPolicy.from_env())
    except (ValueError, TypeError, OverflowError):
        return {"compatible": False, "reason": "INVALID_ROUTE_DATA"}


def save_logistics_route_plan(cursor, profile, route, stops, plan):
    """Persist an already validated plan inside the caller's transaction."""
    if route is None:
        cursor.execute("""
            INSERT INTO logistics_routes
                (logistics_id, status, current_load_kg, planned_distance_km, route_version)
            VALUES (%s, 'ACTIVE', 0, %s, 1)
        """, (profile["logistics_id"], plan["planned_distance_km"]))
        route_id = cursor.lastrowid
    else:
        route_id = route["route_id"]
        cursor.execute("""
            UPDATE logistics_routes SET planned_distance_km = %s,
                route_version = route_version + 1 WHERE route_id = %s
        """, (plan["planned_distance_km"], route_id))
    completed = [stop for stop in stops if stop["status"] == "COMPLETED"]
    base = max((stop["sequence_no"] for stop in completed), default=0)
    # Move the planned suffix clear first, preserving completed IDs/sequences.
    # This also works if a unique route/sequence index is added later.
    offset = max((stop["sequence_no"] for stop in stops), default=0) + len(plan["stops"]) + 1
    cursor.execute("""
        UPDATE logistics_route_stops SET sequence_no = sequence_no + %s
        WHERE route_id = %s AND status = 'PLANNED' ORDER BY sequence_no DESC
    """, (offset, route_id))
    for sequence, stop in enumerate(plan["stops"], base + 1):
        if stop["kind"] == "EXISTING":
            cursor.execute("""
                UPDATE logistics_route_stops SET sequence_no = %s
                WHERE stop_id = %s AND route_id = %s AND status = 'PLANNED'
            """, (sequence, stop["stop_id"], route_id))
        else:
            cursor.execute("""
                INSERT INTO logistics_route_stops
                    (route_id, order_id, stop_type, sequence_no, latitude,
                     longitude, address, quantity_delta, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PLANNED')
            """, (route_id, stop["order_id"], stop["stop_type"], sequence,
                  stop["latitude"], stop["longitude"], stop["address"], stop["quantity_delta"]))
    return route_id


@app.route("/logistics/available-requests")
def logistics_available_requests():

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    try:

        with logistics_cursor() as cursor:

            # -------------------------------------------------
            # USER
            # -------------------------------------------------

            cursor.execute(
                """
                SELECT
                    id,
                    name,
                    email,
                    phone,
                    role,
                    state,
                    district,
                    market
                FROM users
                WHERE id = %s
                """,
                (user_id,)
            )

            user = cursor.fetchone()

            if not user:
                session.clear()
                return redirect(url_for("login"))

            if user["role"] != "Logistics":
                return "Access denied", 403


            # -------------------------------------------------
            # LOGISTICS PROFILE
            # -------------------------------------------------

            profile = get_logistics_profile(
                cursor,
                user_id
            )

            if not profile:

                available_requests = []

            else:

                # -------------------------------------------------
                # AVAILABLE REQUESTS FOR THIS DRIVER
                # -------------------------------------------------

                cursor.execute(
                    """
                    SELECT

                        lor.request_id,
                        lor.status AS request_status,
                        lor.created_at AS request_created_at,

                        o.order_id,
                        o.quantity,
                        o.product_price,
                        o.product_total,
                        o.distance_km,
                        o.estimated_logistics_cost,
                        o.total_amount,
                        o.status AS order_status,

                        o.pickup_house_no,
                        o.pickup_street,
                        o.pickup_village_city,
                        o.pickup_district,
                        o.pickup_state,
                        o.pickup_address,
                        o.pickup_latitude,
                        o.pickup_longitude,

                        o.delivery_house_no,
                        o.delivery_street,
                        o.delivery_village_city,
                        o.delivery_pincode,
                        o.delivery_district,
                        o.delivery_state,
                        o.delivery_address,
                        o.delivery_latitude,
                        o.delivery_longitude,

                        p.crop_name,
                        p.product_image,

                        farmer.name AS farmer_name,
                        farmer.phone AS farmer_phone,

                        buyer.name AS buyer_name,

                        lp.vehicle_number,
                        lp.vehicle_type,
                        lp.vehicle_capacity

                    FROM logistics_order_requests lor

                    INNER JOIN orders o
                        ON o.order_id = lor.order_id

                    INNER JOIN products p
                        ON p.product_id = o.product_id

                    INNER JOIN users farmer
                        ON farmer.id = o.farmer_id

                    INNER JOIN users buyer
                        ON buyer.id = o.buyer_id

                    INNER JOIN logistics_profiles lp
                        ON lp.logistics_id =
                           lor.logistics_id

                    WHERE lor.logistics_id = %s

                      AND lor.status = 'PENDING'
                      AND lp.availability = 'ONLINE'
                      AND lp.vehicle_capacity >= o.quantity
                      AND o.quantity > 0
                      AND o.assigned_logistics_id IS NULL

                      AND o.status =
                          'PENDING_LOGISTICS'

                    ORDER BY
                        lor.created_at DESC
                    """,
                    (
                        profile["logistics_id"],
                    )
                )

                available_requests = (
                    cursor.fetchall()
                )

                route, stops = get_logistics_route(cursor, profile["logistics_id"])
                if has_unrouted_deliveries(cursor, profile["logistics_id"]):
                    available_requests = []
                else:
                    available_requests = [
                        order for order in available_requests
                        if logistics_route_plan(profile, order, route, stops)["compatible"]
                    ]


    except (mysql.connector.Error, ValueError):

        app.logger.exception(
            "Unable to load available "
            "logistics requests"
        )

        return (
            "Unable to load available "
            "requests. Please try again.",
            503
        )


    user.update(
        profile
        or dict.fromkeys(
            (
                "logistics_id",
                "vehicle_number",
                "vehicle_type",
                "vehicle_capacity",
                "availability",
                "current_latitude",
                "current_longitude",
            )
        )
    )


    response = make_response(
        render_template(
            "available-request.html",
            user=user,
            available_requests=(
                available_requests
            ),
        )
    )

    response.headers[
        "Cache-Control"
    ] = "no-store"

    return response

@app.route(
    "/logistics/available-requests/"
    "<int:order_id>/accept",
    methods=["POST"]
)
def accept_logistics_request(order_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))

    try:
        with logistics_cursor() as cursor:
            # Use the same driver lock as profile and availability updates.
            cursor.execute("SELECT role FROM users WHERE id = %s FOR UPDATE", (user_id,))
            user = cursor.fetchone()
            if not user:
                session.clear()
                return redirect(url_for("login"))
            if user["role"] != "Logistics":
                return "Access denied", 403

            # Keep all acceptance reads current while waiting for another accept.
            profile = get_logistics_profile(cursor, user_id, for_update=True)
            if not profile:
                return redirect("/logistics")
            logistics_id = profile["logistics_id"]

            # Serialize competing accepts before checking the invitation.
            cursor.execute("""
                SELECT order_id, status, assigned_logistics_id, logistics_route_id, quantity,
                       pickup_latitude, pickup_longitude, pickup_address,
                       delivery_latitude, delivery_longitude, delivery_address
                FROM orders WHERE order_id = %s FOR UPDATE
            """, (order_id,))
            order = cursor.fetchone()
            if not order:
                return redirect("/logistics/available-requests?message=Order+not+found")
            if (order["status"] != "PENDING_LOGISTICS" or order["assigned_logistics_id"] is not None
                    or order["logistics_route_id"] is not None):
                return redirect(
                    "/logistics/available-requests?message=This+request+is+no+longer+available"
                )

            cursor.execute("""
                SELECT request_id, status FROM logistics_order_requests
                WHERE order_id = %s AND logistics_id = %s FOR UPDATE
            """, (order_id, logistics_id))
            invitation = cursor.fetchone()
            if not invitation or invitation["status"] != "PENDING":
                return redirect(
                    "/logistics/available-requests?message=This+request+is+no+longer+available"
                )
            if profile["availability"] != "ONLINE":
                return redirect(
                    "/logistics/available-requests?message=Go+online+before+accepting+a+request"
                )
            if has_unrouted_deliveries(cursor, logistics_id, for_update=True):
                return redirect(
                    "/logistics/available-requests?message=Finish+existing+deliveries+before+starting+a+new+route"
                )
            route, stops = get_logistics_route(cursor, logistics_id, for_update=True)
            plan = logistics_route_plan(profile, order, route, stops)
            if not plan["compatible"]:
                return redirect(
                    "/logistics/available-requests?message=This+request+does+not+fit+your+current+route+or+capacity"
                )

            route_id = save_logistics_route_plan(cursor, profile, route, stops, plan)
            cursor.execute("""
                UPDATE orders
                SET assigned_logistics_id = %s, logistics_route_id = %s,
                    logistics_assigned_at = CURRENT_TIMESTAMP,
                    status = 'LOGISTICS_ASSIGNED'
                WHERE order_id = %s
            """, (logistics_id, route_id, order_id))
            cursor.execute("""
                UPDATE logistics_order_requests
                SET status = 'ACCEPTED', responded_at = CURRENT_TIMESTAMP
                WHERE order_id = %s AND logistics_id = %s AND status = 'PENDING'
            """, (order_id, logistics_id))
            cursor.execute("""
                UPDATE logistics_order_requests
                SET status = 'EXPIRED', responded_at = CURRENT_TIMESTAMP
                WHERE order_id = %s AND logistics_id <> %s AND status = 'PENDING'
            """, (order_id, logistics_id))
        # Assignment, route/stops and invitation changes commit together.
        return redirect(
            "/logistics/available-requests?message=Delivery+request+accepted+successfully"
        )
    except (mysql.connector.Error, ValueError):
        app.logger.exception("Unable to accept logistics request")
        return redirect(
            "/logistics/available-requests?message=Unable+to+accept+delivery+request"
        )


@app.route(
    "/logistics/available-requests/"
    "<int:order_id>/reject",
    methods=["POST"]
)
def reject_logistics_request(order_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect(url_for("login"))


    try:

        with logistics_cursor() as cursor:

            cursor.execute(
                """
                SELECT
                    lp.logistics_id

                FROM logistics_profiles lp

                INNER JOIN users u
                    ON u.id = lp.user_id

                WHERE lp.user_id = %s
                  AND u.role = 'Logistics'
                """,
                (user_id,)
            )

            logistics = cursor.fetchone()


            if not logistics:

                return redirect(
                    "/logistics"
                )


            cursor.execute(
                """
                UPDATE logistics_order_requests

                SET
                    status = 'REJECTED',
                    responded_at =
                        CURRENT_TIMESTAMP

                WHERE order_id = %s
                  AND logistics_id = %s
                  AND status = 'PENDING'
                """,
                (
                    order_id,
                    logistics[
                        "logistics_id"
                    ],
                )
            )


            if cursor.rowcount == 0:
                return redirect(
                    "/logistics/available-requests?message=This+request+is+no+longer+available"
                )

    except mysql.connector.Error:

        app.logger.exception(
            "Unable to reject "
            "logistics request"
        )

        return redirect(
            "/logistics/available-requests"
            "?message=Unable+to+reject+"
            "delivery+request"
        )


    return redirect(
        "/logistics/available-requests"
        "?message=Delivery+request+rejected"
    )




@app.route("/logistics/update-profile", methods=["POST"])
@logistics_api
def update_logistics_profile(cursor, user_id):
    vehicle_number = request.form.get("vehicle_number", "").strip()
    vehicle_type = request.form.get("vehicle_type", "").strip()
    capacity = request.form.get("vehicle_capacity", "").strip()
    if not vehicle_number or not vehicle_type or not capacity:
        return {"success": False, "message": "Please fill all vehicle details."}, 400
    if vehicle_type not in LOGISTICS_VEHICLE_TYPES:
        return {"success": False, "message": "Please select a valid vehicle type."}, 400
    try:
        capacity = float(capacity)
        if not math.isfinite(capacity) or capacity <= 0:
            raise ValueError
    except (ValueError, OverflowError):
        return {"success": False, "message": "Invalid vehicle capacity."}, 400
    profile = get_logistics_profile(cursor, user_id, for_update=True)
    if profile:
        try:
            route, stops = get_logistics_route(cursor, profile["logistics_id"], for_update=True)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}, 409
        if route:
            load = logistics_weight(route["current_load_kg"])
            peak = load
            for stop in stops:
                if stop["status"] == "PLANNED":
                    load += logistics_weight(stop["quantity_delta"])
                    peak = max(peak, load)
            if Decimal(str(capacity)) < peak:
                return {"success": False, "message": "Vehicle capacity cannot be lower than the load already planned for your route."}, 409
        cursor.execute("""
            UPDATE logistics_profiles
            SET vehicle_number = %s, vehicle_type = %s, vehicle_capacity = %s
            WHERE user_id = %s
        """, (vehicle_number, vehicle_type, capacity, user_id))
    else:
        cursor.execute("""
            INSERT INTO logistics_profiles
                (user_id, vehicle_number, vehicle_type, vehicle_capacity, availability)
            VALUES (%s, %s, %s, %s, 'OFFLINE')
        """, (user_id, vehicle_number, vehicle_type, capacity))
    return {
        "success": True,
        "message": "Vehicle information saved successfully.",
        "vehicle": {
            "vehicle_number": vehicle_number,
            "vehicle_type": vehicle_type,
            "vehicle_capacity": capacity,
        },
    }


@app.route("/logistics/location")
@logistics_api
def get_logistics_location(cursor, user_id):
    # There is deliberately no user or vehicle ID parameter: drivers see only themselves.
    return logistics_location_state(get_logistics_profile(cursor, user_id))


@app.route("/logistics/toggle-availability", methods=["POST"])
@logistics_api
def toggle_logistics_availability(cursor, user_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"success": False, "message": "A JSON object is required."}, 400
    availability = data.get("availability")
    if availability not in ("ONLINE", "OFFLINE"):
        return {"success": False, "message": "Invalid availability status."}, 400
    profile = get_logistics_profile(cursor, user_id)
    if not profile:
        return {"success": False, "message": "Please save vehicle information first."}, 400
    state = logistics_location_state(profile)
    age = state["last_seen_seconds"]
    if availability == "ONLINE" and (state["location"] is None or age is None or age > LOCATION_STALE_SECONDS):
        return {"success": False, "message": "Save a current GPS location before going online."}, 400
    cursor.execute("""
        UPDATE logistics_profiles SET availability = %s WHERE user_id = %s
    """, (availability, user_id))
    profile["availability"] = availability
    return logistics_location_state(profile)


@app.route("/logistics/update-location", methods=["POST"])
@logistics_api
def update_logistics_location(cursor, user_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"success": False, "message": "A JSON object is required."}, 400
    latitude, longitude = data.get("latitude"), data.get("longitude")
    if latitude is None or longitude is None:
        return {"success": False, "message": "Location coordinates are required."}, 400
    try:
        if isinstance(latitude, bool) or isinstance(longitude, bool):
            raise ValueError
        latitude, longitude = float(latitude), float(longitude)
        if not math.isfinite(latitude) or not math.isfinite(longitude) or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        return {"success": False, "message": "Invalid location coordinates."}, 400
    profile = get_logistics_profile(cursor, user_id)
    if not profile:
        return {"success": False, "message": "Please save vehicle information first."}, 404
    cursor.execute("""
        UPDATE logistics_profiles
        SET current_latitude = %s, current_longitude = %s,
            location_updated_at = CURRENT_TIMESTAMP
        WHERE user_id = %s
    """, (latitude, longitude, user_id))
    # An unchanged coordinate update is still a successful heartbeat.
    profile.update(current_latitude=latitude, current_longitude=longitude, location_age_seconds=0)
    state = logistics_location_state(profile)
    state.update(latitude=latitude, longitude=longitude)
    return state


@app.route("/reverse-geocode")
def reverse_geocode():

    lat = request.args.get("lat")
    lng = request.args.get("lng")

    if not lat or not lng:
        return {
            "error": "Latitude and longitude are required"
        }, 400

    try:
        lat = float(lat)
        lng = float(lng)

        if lat < -90 or lat > 90 or lng < -180 or lng > 180:
            return {
                "error": "Invalid coordinates"
            }, 400

        url = "https://nominatim.openstreetmap.org/reverse"

        params = {
            "format": "json",
            "lat": lat,
            "lon": lng,
            "zoom": 18,
            "addressdetails": 1
        }

        headers = {
            "User-Agent": "AgriConnect/1.0"
        }

        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=10
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:

        print("Reverse geocoding error:", e)

        return {
            "error": "Unable to find address"
        }, 500


@app.route("/search-location")
def search_location():

    query = request.args.get("q")

    if not query:
        return {
            "error": "Search query is required"
        }, 400

    try:

        url = "https://nominatim.openstreetmap.org/search"

        params = {
            "format": "json",
            "q": query,
            "limit": 1
        }

        headers = {
            "User-Agent": "AgriConnect/1.0"
        }

        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=10
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:

        print("Location search error:", e)

        return {
            "error": "Unable to search location"
        }, 500

from buyer_routes import register_buyer_routes
from delivery_routes import register_delivery_routes, delivery_list, delivery_csrf_token


def delivery_label(row):
    # Keep the existing template's plain-text list usable during the design work.
    status = row["status"].replace("_", " ").title()
    return f"Order #{row['order_id']} · {row.get('crop_name') or 'Produce'} · {row['quantity']} kg · {status}"

register_buyer_routes(app, logistics_cursor, logistics_location_state)
register_delivery_routes(app, logistics_api, get_logistics_profile, get_logistics_route)


if __name__ == "__main__":

    app.run( debug=True)
