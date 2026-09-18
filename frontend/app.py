from flask import Flask, render_template, request, session, redirect
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

app.secret_key = "my-secret-key"

SENDER_EMAIL = "smart.dustbin.service@gmail.com"

SENDER_PASSWORD = "mizn jpba ubfs luid"

db = mysql.connector.connect(
    host="localhost",
    user="root",
    password="123456",
    database="agriconnect"
)

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
                    "index.html",
                    message="Unknown user role"
                )

        else:

            return render_template(
                "index.html",
                message="Wrong email or password"
            )

    return render_template("index.html")


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

            print(
                "Connecting to Gmail..."
            )

            with smtplib.SMTP_SSL(
                "smtp.gmail.com",
                465,
                timeout=30
            ) as smtp:

                print(
                    "Connected to Gmail"
                )

                smtp.login(
                    SENDER_EMAIL,
                    SENDER_PASSWORD
                )

                print(
                    "Gmail login successful"
                )

                smtp.send_message(msg)

                print(
                    "OTP sent successfully"
                )

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

            return redirect("/")

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

        return redirect("/")

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

        return redirect("/")

    return render_template(
        "seller-dashboard.html",
        user=user
    )


@app.route("/my-products")
def my_products():

    user_id = session.get("user_id")

    if not user_id:

        return redirect("/")

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
        return redirect("/")


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

            return redirect("/")


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
        return redirect("/")

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
        return redirect("/")

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

        return redirect("/")

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

        return redirect("/")

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


@app.route("/buyer")
def buyer():

    user_id = session.get("user_id")

    if not user_id:

        return redirect("/")

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

        return redirect("/")

    return render_template(
        "buyer-dashboard.html",
        user=user
    )



@app.route("/browse-products")
def browse_products():
    user_id = session.get("user_id")

    if not user_id:
        return redirect("/")

    cursor = db.cursor(dictionary=True)

    query = """
        SELECT *
        FROM products
        WHERE quantity > 0
        ORDER BY created_at DESC
    """

    cursor.execute(query)
    products = cursor.fetchall()
    cursor.close()

    return render_template(
        "browse-products.html",
        products=products
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



@app.route("/buy-product/<int:product_id>")
def buy_product(product_id):

    user_id = session.get("user_id")

    if not user_id:
        return redirect("/")

    cursor = db.cursor(dictionary=True)

    query = """
        SELECT *
        FROM products
        WHERE product_id = %s
          AND quantity > 0
    """

    cursor.execute(query, (product_id,))
    product = cursor.fetchone()

    cursor.close()

    if not product:
        return "Product not available", 404

    return render_template(
        "buy-product.html",
        product=product
    )





@app.route("/government-schemes")
def government_schemes():

    # Logged-in user ki ID
    user_id = session.get("user_id")

    # Login nahi hai
    if not user_id:
        return redirect("/")

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
        return redirect("/")

    # User information scheme.html ko bhejo
    return render_template(
        "scheme.html",
        user=user
    )




@app.route("/logistics")
def logistics_dashboard():

    user_id = session.get("user_id")

    if not user_id:
        return redirect("/")

    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            u.id,
            u.name,
            u.email,
            u.phone,
            u.role,
            u.state,
            u.district,
            u.market,

            lp.logistics_id,
            lp.vehicle_number,
            lp.vehicle_type,
            lp.vehicle_capacity,
            lp.availability,
            lp.current_latitude,
            lp.current_longitude,
            lp.location_updated_at

        FROM users u

        LEFT JOIN logistics_profiles lp
            ON lp.user_id = u.id

        WHERE u.id = %s
    """, (user_id,))

    user = cursor.fetchone()

    cursor.close()

    if not user:
        session.clear()
        return redirect("/")

    # Security
    if user["role"] != "Logistics":
        return "Access denied", 403

    return render_template(
        "logistics-dashboard.html",
        user=user
    )



@app.route("/logistics/update-profile", methods=["POST"])
def update_logistics_profile():

    user_id = session.get("user_id")

    if not user_id:
        return {
            "success": False,
            "message": "Login required"
        }, 401

    cursor = db.cursor(dictionary=True)

    # Check role
    cursor.execute("""
        SELECT role
        FROM users
        WHERE id = %s
    """, (user_id,))

    user = cursor.fetchone()

    if not user or user["role"] != "Logistics":
        cursor.close()

        return {
            "success": False,
            "message": "Access denied"
        }, 403

    vehicle_number = request.form.get(
        "vehicle_number", ""
    ).strip()

    vehicle_type = request.form.get(
        "vehicle_type", ""
    ).strip()

    vehicle_capacity = request.form.get(
        "vehicle_capacity", ""
    ).strip()

    if not vehicle_number or not vehicle_type or not vehicle_capacity:

        cursor.close()

        return {
            "success": False,
            "message": "Please fill all vehicle details."
        }, 400

    try:

        vehicle_capacity = float(vehicle_capacity)

        if vehicle_capacity <= 0:
            raise ValueError

    except ValueError:

        cursor.close()

        return {
            "success": False,
            "message": "Invalid vehicle capacity."
        }, 400

    # Check whether profile already exists
    cursor.execute("""
        SELECT logistics_id
        FROM logistics_profiles
        WHERE user_id = %s
    """, (user_id,))

    profile = cursor.fetchone()

    if profile:

        cursor.execute("""
            UPDATE logistics_profiles

            SET
                vehicle_number = %s,
                vehicle_type = %s,
                vehicle_capacity = %s

            WHERE user_id = %s
        """, (
            vehicle_number,
            vehicle_type,
            vehicle_capacity,
            user_id
        ))

    else:

        cursor.execute("""
            INSERT INTO logistics_profiles
            (
                user_id,
                vehicle_number,
                vehicle_type,
                vehicle_capacity,
                availability
            )

            VALUES
            (
                %s,
                %s,
                %s,
                %s,
                'OFFLINE'
            )
        """, (
            user_id,
            vehicle_number,
            vehicle_type,
            vehicle_capacity
        ))

    db.commit()
    cursor.close()

    return {
        "success": True,
        "message": "Vehicle information saved successfully."
    }



@app.route("/logistics/toggle-availability", methods=["POST"])
def toggle_logistics_availability():

    user_id = session.get("user_id")

    if not user_id:
        return {
            "success": False,
            "message": "Login required"
        }, 401

    data = request.get_json(silent=True) or {}

    availability = data.get("availability")

    if availability not in ["ONLINE", "OFFLINE"]:
        return {
            "success": False,
            "message": "Invalid availability status."
        }, 400

    cursor = db.cursor(dictionary=True)

    # Check logistics profile
    cursor.execute("""
        SELECT logistics_id
        FROM logistics_profiles
        WHERE user_id = %s
    """, (user_id,))

    profile = cursor.fetchone()

    if not profile:
        cursor.close()

        return {
            "success": False,
            "message": "Please save vehicle information first."
        }, 400

    cursor.execute("""
        UPDATE logistics_profiles

        SET availability = %s

        WHERE user_id = %s
    """, (
        availability,
        user_id
    ))

    db.commit()
    cursor.close()

    return {
        "success": True,
        "availability": availability
    }


@app.route("/logistics/update-location", methods=["POST"])
def update_logistics_location():

    user_id = session.get("user_id")

    if not user_id:
        return {
            "success": False,
            "message": "Login required"
        }, 401

    data = request.get_json(silent=True) or {}

    latitude = data.get("latitude")
    longitude = data.get("longitude")

    if latitude is None or longitude is None:

        return {
            "success": False,
            "message": "Location coordinates are required."
        }, 400

    try:

        latitude = float(latitude)
        longitude = float(longitude)

        if not (-90 <= latitude <= 90):
            raise ValueError

        if not (-180 <= longitude <= 180):
            raise ValueError

    except (ValueError, TypeError):

        return {
            "success": False,
            "message": "Invalid location coordinates."
        }, 400

    cursor = db.cursor()

    cursor.execute("""
        UPDATE logistics_profiles

        SET
            current_latitude = %s,
            current_longitude = %s,
            location_updated_at = CURRENT_TIMESTAMP

        WHERE user_id = %s
    """, (
        latitude,
        longitude,
        user_id
    ))

    db.commit()

    updated_rows = cursor.rowcount

    cursor.close()

    if updated_rows == 0:

        return {
            "success": False,
            "message": "Logistics profile not found."
        }, 404

    return {
        "success": True,
        "latitude": latitude,
        "longitude": longitude
    }


    
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

if __name__ == "__main__":

    app.run( debug=True)



    