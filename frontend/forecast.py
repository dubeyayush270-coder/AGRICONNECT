import pandas as pd
import numpy as np

from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error


# ==========================================
# 1. Load Dataset
# ==========================================

data = pd.read_csv("demand_data.csv")

print(data.head())

print("Total rows:", len(data))


# ==========================================
# 2. Basic Dataset Information
# ==========================================

print("\nCrops:")
print(data["crop_name"].unique())

print("\nMarkets:")
print(data["market"].unique())

print("\nCrop-Market combinations:")
print(
    data.groupby(["market", "crop_name"]).size()
)


# ==========================================
# 3. Convert Date
# ==========================================

data["date"] = pd.to_datetime(data["date"])


# ==========================================
# 4. Create Date Features
# ==========================================

data["year"] = data["date"].dt.year
data["month"] = data["date"].dt.month
data["day"] = data["date"].dt.day
data["day_of_week"] = data["date"].dt.dayofweek

print("\nDate features:")

print(
    data[
        [
            "date",
            "year",
            "month",
            "day",
            "day_of_week"
        ]
    ].head()
)


# ==========================================
# 5. Sort Data
# ==========================================

data = data.sort_values(
    ["market", "crop_name", "date"]
)


# ==========================================
# 6. Create Lag Features
# ==========================================

# Previous day's demand
data["demand_lag_1"] = (
    data
    .groupby(["market", "crop_name"])["demand_quantity"]
    .shift(1)
)


# Demand 7 days ago
data["demand_lag_7"] = (
    data
    .groupby(["market", "crop_name"])["demand_quantity"]
    .shift(7)
)


print("\nLag features:")

print(
    data[
        [
            "date",
            "market",
            "crop_name",
            "demand_quantity",
            "demand_lag_1",
            "demand_lag_7"
        ]
    ].head(10)
)


# ==========================================
# 7. Create 7-Day Rolling Average
# ==========================================

data["demand_rolling_7"] = (
    data
    .groupby(["market", "crop_name"])["demand_quantity"]
    .transform(
        lambda x: x.shift(1).rolling(7).mean()
    )
)


print("\nRolling average:")

print(
    data[
        [
            "date",
            "market",
            "crop_name",
            "demand_quantity",
            "demand_lag_1",
            "demand_lag_7",
            "demand_rolling_7"
        ]
    ].head(12)
)


# ==========================================
# 8. Remove Missing Values
# ==========================================

data = data.dropna(
    subset=[
        "demand_lag_1",
        "demand_lag_7",
        "demand_rolling_7"
    ]
)


print("\nAfter removing missing values:")

print("Total rows:", len(data))

print(data.isnull().sum())


# ==========================================
# 9. Create Features (X)
# ==========================================

X = data[
    [
        "market",
        "crop_name",
        "year",
        "month",
        "day",
        "day_of_week",
        "demand_lag_1",
        "demand_lag_7",
        "demand_rolling_7"
    ]
]


# Target
y = data["demand_quantity"]


print("\nX:")
print(X.head())

print("\ny:")
print(y.head())


# ==========================================
# 10. One-Hot Encoding
# ==========================================

X = pd.get_dummies(
    X,
    columns=[
        "market",
        "crop_name"
    ],
    dtype=int
)


print("\nEncoded X:")
print(X.head())

print("\nFeature columns:")
print(X.columns.tolist())


# ==========================================
# 11. Date-Based Train-Test Split
# ==========================================

split_date = pd.Timestamp("2026-05-03")


train_mask = data["date"] < split_date
test_mask = data["date"] >= split_date


X_train = X.loc[train_mask]
X_test = X.loc[test_mask]

y_train = y.loc[train_mask]
y_test = y.loc[test_mask]


# ==========================================
# 12. Check Training and Testing Data
# ==========================================

print("\nTraining data:")

print("X_train:", X_train.shape)
print("y_train:", y_train.shape)


print("\nTesting data:")

print("X_test:", X_test.shape)
print("y_test:", y_test.shape)


# ==========================================
# 13. Check Date Ranges
# ==========================================

print("\nTraining date range:")

print(
    data.loc[train_mask, "date"].min()
)

print(
    data.loc[train_mask, "date"].max()
)


print("\nTesting date range:")

print(
    data.loc[test_mask, "date"].min()
)

print(
    data.loc[test_mask, "date"].max()
)


# ==========================================
# 14. Create ML Model
# ==========================================

model = RandomForestRegressor(
    n_estimators=200,
    random_state=42,
    n_jobs=-1
)


# ==========================================
# 15. Train Model
# ==========================================

model.fit(X_train, y_train)

print("\nModel training completed!")


# ==========================================
# 16. Make Predictions
# ==========================================

y_pred = model.predict(X_test)

print("\nPredictions:")
print(y_pred[:10])


# ==========================================
# 17. Model Evaluation
# ==========================================

mae = mean_absolute_error(
    y_test,
    y_pred
)

rmse = np.sqrt(
    mean_squared_error(
        y_test,
        y_pred
    )
)


print("\nModel Evaluation:")

print("MAE:", mae)
print("RMSE:", rmse)


# ==========================================
# 18. Compare Actual vs Predicted Demand
# ==========================================

results = data.loc[
    test_mask,
    [
        "date",
        "market",
        "crop_name",
        "demand_quantity"
    ]
].copy()


results["predicted_demand"] = y_pred


print("\nActual vs Predicted Demand:")

print(
    results.head(10)
)


# ==========================================
# 19. 7-Day Forecast Function
# ==========================================

def forecast_demand(selected_market, selected_crop):

    # --------------------------------------
    # Selected market and crop ka data
    # --------------------------------------

    history = data[
        (data["market"] == selected_market) &
        (data["crop_name"] == selected_crop)
    ].copy()


    # Check whether data exists
    if history.empty:

        print(
            "\nNo data found for:",
            selected_market,
            selected_crop
        )

        return None


    # Sort by date
    history = history.sort_values("date")


    # --------------------------------------
    # Last available date
    # --------------------------------------

    last_date = history["date"].max()


    print("\nSelected Market:", selected_market)

    print("Selected Crop:", selected_crop)

    print("Last available date:", last_date)


    # --------------------------------------
    # Last 7 days demand
    # --------------------------------------

    demand_history = list(
        history["demand_quantity"].tail(7)
    )


    # --------------------------------------
    # Check sufficient history
    # --------------------------------------

    if len(demand_history) < 7:

        print(
            "\nNot enough historical data "
            "for 7-day forecasting."
        )

        return None


    # --------------------------------------
    # Store forecast results
    # --------------------------------------

    forecast_results = []


    # ======================================
    # Generate 7 Future Days
    # ======================================

    for i in range(1, 8):


        # Future date
        future_date = (
            last_date +
            pd.Timedelta(days=i)
        )


        # ----------------------------------
        # Create lag features
        # ----------------------------------

        demand_lag_1 = demand_history[-1]

        demand_lag_7 = demand_history[-7]

        demand_rolling_7 = (
            sum(demand_history[-7:]) / 7
        )


        # ----------------------------------
        # Create empty feature row
        # ----------------------------------

        future_data = pd.DataFrame(
            0,
            index=[0],
            columns=X.columns
        )


        # ----------------------------------
        # Date features
        # ----------------------------------

        future_data["year"] = future_date.year

        future_data["month"] = future_date.month

        future_data["day"] = future_date.day

        future_data["day_of_week"] = (
            future_date.dayofweek
        )


        # ----------------------------------
        # Demand features
        # ----------------------------------

        future_data["demand_lag_1"] = (
            demand_lag_1
        )

        future_data["demand_lag_7"] = (
            demand_lag_7
        )

        future_data["demand_rolling_7"] = (
            demand_rolling_7
        )


        # ----------------------------------
        # Market
        # ----------------------------------

        market_column = (
            "market_" +
            selected_market
        )


        if market_column in future_data.columns:

            future_data.loc[
                0,
                market_column
            ] = 1


        # ----------------------------------
        # Crop
        # ----------------------------------

        crop_column = (
            "crop_name_" +
            selected_crop
        )


        if crop_column in future_data.columns:

            future_data.loc[
                0,
                crop_column
            ] = 1


        # ----------------------------------
        # Prediction
        # ----------------------------------

        prediction = model.predict(
            future_data
        )[0]


        # ----------------------------------
        # Add prediction to history
        # ----------------------------------

        demand_history.append(
            prediction
        )


        # ----------------------------------
        # Save result
        # ----------------------------------

        forecast_results.append(
            {
                "date": future_date.date(),

                "market": selected_market,

                "crop": selected_crop,

                "predicted_demand":
                    round(prediction, 2)
            }
        )


    # ======================================
    # Convert results to DataFrame
    # ======================================

    forecast_df = pd.DataFrame(
        forecast_results
    )


    print(
        "\n7-Day Future Demand Forecast:"
    )

    print(forecast_df)


    return forecast_df

