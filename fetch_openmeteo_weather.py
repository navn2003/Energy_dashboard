import sys
import requests
import mysql.connector
import pandas as pd

# ===================== CONFIG =====================
DB_CONFIG = {
    "host": "65.1.28.178",
    "port": 3306,
    "user": "energy",
    "password": "Energy@123",
    "database": "energy_monitor",
    "connect_timeout": 30,
}

TARGET_PLANTS = [
    "DSS00011", "DSS00015", "DSS00016", "DSS00017", "DSS00018",
    "DSS00019", "DSS00020", "DSS00021", "DSS00035", "DSS00037",
]

# Allow the dashboard to pass the date dynamically!
if len(sys.argv) > 1:
    FORECAST_DATE = sys.argv[1]
else:
    FORECAST_DATE = "2026-05-26"

# Change date here when you want another forecast date
FORECAST_DATE = "2026-05-26"


# ===================== DB CONNECTION =====================

def get_connection():
    return mysql.connector.connect(**DB_CONFIG)


# ===================== PLANT LOCATION =====================

def get_plant_locations(conn):
    placeholders = ",".join(["%s"] * len(TARGET_PLANTS))

    query = f"""
        SELECT 
            dss_id,
            dss_name,
            latitude,
            longitude
        FROM plant_master
        WHERE dss_id IN ({placeholders})
    """

    df = pd.read_sql(query, conn, params=TARGET_PLANTS)

    if df.empty:
        raise Exception("No plants found in plant_master.")

    if "latitude" not in df.columns:
        raise Exception("latitude column not found in plant_master.")

    if "longitude" not in df.columns:
        raise Exception("longitude column not found in plant_master.")

    df = df.dropna(subset=["latitude", "longitude"])

    if df.empty:
        raise Exception("No plants have valid latitude and longitude.")

    return df


# ===================== OPEN-METEO FETCH =====================

def fetch_openmeteo_15min(latitude, longitude, date):
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "minutely_15": ",".join([
            "temperature_2m",
            "shortwave_radiation",
            "direct_normal_irradiance",
            "wind_speed_10m",
        ]),
        "start_date": date,
        "end_date": date,
        "timezone": "Asia/Kolkata",
    }

    response = requests.get(url, params=params, timeout=60)
    response.raise_for_status()

    data = response.json()

    if "minutely_15" not in data:
        raise Exception(f"No minutely_15 data returned from Open-Meteo: {data}")

    m = data["minutely_15"]

    required_keys = [
        "time",
        "temperature_2m",
        "shortwave_radiation",
        "direct_normal_irradiance",
        "wind_speed_10m",
    ]

    for key in required_keys:
        if key not in m:
            raise Exception(f"Missing '{key}' in Open-Meteo response.")

    df = pd.DataFrame({
        "block_time": pd.to_datetime(m["time"]),
        "temperature_2m": m["temperature_2m"],
        "shortwave_radiation": m["shortwave_radiation"],
        "direct_normal_irradiance": m["direct_normal_irradiance"],
        "wind_speed_10m": m["wind_speed_10m"],
    })

    return df


# ===================== BLOCK NUMBER =====================

def get_block_no(dt):
    """
    15-minute block number:
    00:00 = 1
    00:15 = 2
    00:30 = 3
    ...
    23:45 = 96
    """
    return (dt.hour * 4) + (dt.minute // 15) + 1


# ===================== DELETE OLD WEATHER =====================

def delete_existing_weather(conn, dss_id, date):
    sql = """
        DELETE FROM plant_weather_15min
        WHERE dss_id = %s
          AND DATE(block_time) = %s
    """

    cur = conn.cursor()
    cur.execute(sql, (dss_id, date))
    deleted = cur.rowcount
    conn.commit()
    cur.close()

    return deleted


# ===================== INSERT WEATHER =====================

def insert_weather(conn, dss_id, weather_df):
    sql = """
        INSERT INTO plant_weather_15min
        (
            dss_id,
            block_time,
            block_no,
            temperature_2m,
            shortwave_radiation,
            direct_normal_irradiance,
            wind_speed_10m
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """

    rows = []

    for _, r in weather_df.iterrows():
        block_time = pd.to_datetime(r["block_time"])
        block_no = get_block_no(block_time)

        rows.append((
            dss_id,
            block_time.strftime("%Y-%m-%d %H:%M:%S"),
            block_no,
            float(r["temperature_2m"]) if pd.notna(r["temperature_2m"]) else 0.0,
            float(r["shortwave_radiation"]) if pd.notna(r["shortwave_radiation"]) else 0.0,
            float(r["direct_normal_irradiance"]) if pd.notna(r["direct_normal_irradiance"]) else 0.0,
            float(r["wind_speed_10m"]) if pd.notna(r["wind_speed_10m"]) else 0.0,
        ))

    if not rows:
        return 0

    cur = conn.cursor()
    cur.executemany(sql, rows)
    conn.commit()
    cur.close()

    return len(rows)


# ===================== MAIN =====================

def main():
    conn = get_connection()

    try:
        plants = get_plant_locations(conn)

        print("=" * 70)
        print(f"Fetching Open-Meteo weather for date: {FORECAST_DATE}")
        print(f"Total plants: {len(plants)}")
        print("=" * 70)

        for _, plant in plants.iterrows():
            dss_id = plant["dss_id"]
            dss_name = plant["dss_name"]
            latitude = float(plant["latitude"])
            longitude = float(plant["longitude"])

            print()
            print(f"Fetching weather for {dss_id} - {dss_name}")
            print(f"Latitude: {latitude}, Longitude: {longitude}")

            try:
                weather_df = fetch_openmeteo_15min(latitude, longitude, FORECAST_DATE)

                print(f"Rows fetched from Open-Meteo: {len(weather_df)}")

                if len(weather_df) == 0:
                    print(f"No weather data returned for {dss_id}. Skipping.")
                    continue

                deleted = delete_existing_weather(conn, dss_id, FORECAST_DATE)
                print(f"Old rows deleted: {deleted}")

                inserted = insert_weather(conn, dss_id, weather_df)
                print(f"Inserted rows: {inserted}")

            except Exception as e:
                print(f"FAILED for {dss_id}: {e}")

        print()
        print("=" * 70)
        print("Weather fetch completed.")
        print("=" * 70)

    finally:
        conn.close()


if __name__ == "__main__":
    main()