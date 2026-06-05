# nrmse.py
# Calculate NRMSE for May 26 forecast file.
# Terminal output only. No DB insert/update/delete.

from pathlib import Path
import pandas as pd
import pymysql
import numpy as np


# ============================================================
# CONFIG
# ============================================================

DB_CONFIG = {
    "host": "13.205.184.74",
    "user": "normal_access",
    "password": "energyX@123#",
    "database": "engxai_fs",
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}

BASE_DIR = Path(__file__).resolve().parent

FORECAST_EXCEL = BASE_DIR / "forecast_2026-05-26_wide_single_sheet.xlsx"

FIXED_AVC_MW = 776.61

# Force May 26 actual fetch
FORCE_DATE = "2026-05-26"

# Keep empty to auto-select DSS columns from Excel
TARGET_DSS_IDS = [
    # "DSS00011",
    # "DSS00015",
    # "DSS00016",
    # "DSS00017",
    # "DSS00018",
    # "DSS00019",
    # "DSS00020",
    # "DSS00021",
    # "DSS00035",
    # "DSS00037",
]


# ============================================================
# DB
# ============================================================

def get_connection():
    return pymysql.connect(**DB_CONFIG)


def fetch_df(query, params=None):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, params or [])
            rows = cursor.fetchall()
    return pd.DataFrame(rows)


# ============================================================
# HELPERS
# ============================================================

def normalize_text(value):
    return str(value).strip().upper()


def detect_timestamp_column(df):
    possible_cols = [
        "timestamp",
        "TIMESTAMP",
        "datetime",
        "DATETIME",
        "date_time",
        "DATE_TIME",
        "DateTime",
        "Datetime",
        "date",
        "DATE",
        "time",
        "TIME",
    ]

    for col in possible_cols:
        if col in df.columns:
            return col

    return df.columns[0]


def force_forecast_date(forecast_df, force_date):
    force_date_obj = pd.to_datetime(force_date).date()

    forecast_df["TIMESTAMP"] = pd.to_datetime(
        forecast_df["TIMESTAMP"],
        errors="coerce"
    )

    forecast_df = forecast_df.dropna(subset=["TIMESTAMP"]).copy()

    forecast_df["TIMESTAMP"] = forecast_df["TIMESTAMP"].apply(
        lambda x: pd.Timestamp.combine(force_date_obj, x.time())
    )

    start_time = pd.Timestamp(f"{force_date} 00:00:00")
    end_time = pd.Timestamp(f"{force_date} 23:45:00")

    return forecast_df, start_time, end_time


# ============================================================
# LOAD FORECAST EXCEL
# ============================================================

def load_forecast_excel():
    if not FORECAST_EXCEL.exists():
        raise FileNotFoundError(f"Forecast Excel file not found: {FORECAST_EXCEL}")

    xls = pd.ExcelFile(FORECAST_EXCEL)

    print("\nAvailable sheets:")
    print(xls.sheet_names)

    sheet_name = "Forecast" if "Forecast" in xls.sheet_names else xls.sheet_names[0]

    print("\nUsing sheet:", sheet_name)

    df = pd.read_excel(FORECAST_EXCEL, sheet_name=sheet_name)
    df.columns = [str(c).strip() for c in df.columns]

    print("\nExcel columns:")
    print(df.columns.tolist())

    time_col = detect_timestamp_column(df)
    print("\nDetected timestamp column:", time_col)

    df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
    df = df.dropna(subset=[time_col])
    df = df.rename(columns={time_col: "TIMESTAMP"})

    if TARGET_DSS_IDS:
        dss_ids = [normalize_text(x) for x in TARGET_DSS_IDS]
    else:
        dss_ids = [
            normalize_text(col)
            for col in df.columns
            if normalize_text(col).startswith("DSS")
        ]

    if not dss_ids:
        raise ValueError("No DSS columns found in Excel.")

    print("\nDSS columns selected:")
    for dss in dss_ids:
        print(dss)

    missing_cols = [dss for dss in dss_ids if dss not in df.columns]

    if missing_cols:
        raise ValueError(f"These DSS columns are missing in Excel: {missing_cols}")

    for dss in dss_ids:
        df[dss] = pd.to_numeric(df[dss], errors="coerce").fillna(0)

    if "AGGREGATE" in df.columns:
        df["FORECAST_MW"] = pd.to_numeric(
            df["AGGREGATE"],
            errors="coerce"
        ).fillna(0)

        print("\nForecast source: AGGREGATE column")
    else:
        df["FORECAST_MW"] = df[dss_ids].sum(axis=1)

        print("\nForecast source: Sum of DSS columns")

    forecast_df = df[["TIMESTAMP", "FORECAST_MW"] + dss_ids].copy()

    forecast_df, start_time, end_time = force_forecast_date(
        forecast_df,
        FORCE_DATE
    )

    return forecast_df, dss_ids, start_time, end_time


# ============================================================
# FETCH ACTUAL SCADA
# ============================================================

def fetch_actual_from_db(dss_ids, start_time, end_time):
    dss_ids = [normalize_text(x) for x in dss_ids]

    placeholders = ",".join(["%s"] * len(dss_ids))

    query = f"""
        SELECT
            TRIM(UPPER(DSS_ID)) AS DSS_ID,
            TIMESTAMP,
            SCADA_POWER_MW
        FROM DSS_ACTUAL
        WHERE TRIM(UPPER(DSS_ID)) IN ({placeholders})
          AND TIMESTAMP IS NOT NULL
          AND CAST(TIMESTAMP AS CHAR) REGEXP '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'
          AND TIMESTAMP BETWEEN %s AND %s
        ORDER BY TIMESTAMP, DSS_ID
    """

    params = dss_ids + [
        start_time.strftime("%Y-%m-%d %H:%M:%S"),
        end_time.strftime("%Y-%m-%d %H:%M:%S"),
    ]

    actual_df = fetch_df(query, params)

    if actual_df.empty:
        print("\nERROR: No actual SCADA data found in DSS_ACTUAL.")
        print("\nDSS_ID searched:")
        for dss in dss_ids:
            print(dss)

        print("\nTime range searched:")
        print(start_time, "to", end_time)

        raise ValueError("No actual SCADA data found.")

    actual_df["DSS_ID"] = actual_df["DSS_ID"].astype(str).str.strip().str.upper()

    actual_df["TIMESTAMP"] = pd.to_datetime(
        actual_df["TIMESTAMP"],
        errors="coerce"
    )

    before_rows = len(actual_df)
    actual_df = actual_df.dropna(subset=["TIMESTAMP"])
    removed = before_rows - len(actual_df)

    if removed > 0:
        print(f"\nRemoved bad TIMESTAMP rows in script only: {removed}")

    actual_df["SCADA_POWER_MW"] = pd.to_numeric(
        actual_df["SCADA_POWER_MW"],
        errors="coerce"
    ).fillna(0)

    actual_agg = (
        actual_df
        .groupby("TIMESTAMP", as_index=False)["SCADA_POWER_MW"]
        .sum()
        .rename(columns={"SCADA_POWER_MW": "ACTUAL_MW"})
    )

    return actual_df, actual_agg


# ============================================================
# CALCULATE NRMSE
# ============================================================

def calculate_nrmse(forecast_df, actual_agg, avc):
    merged = pd.merge(
        forecast_df[["TIMESTAMP", "FORECAST_MW"]],
        actual_agg,
        on="TIMESTAMP",
        how="inner"
    )

    if merged.empty:
        print("\nForecast timestamp sample:")
        print(forecast_df["TIMESTAMP"].head(10).to_string(index=False))

        print("\nActual timestamp sample:")
        print(actual_agg["TIMESTAMP"].head(10).to_string(index=False))

        raise ValueError("Forecast and actual timestamps are not matching.")

    merged["ERROR_MW"] = merged["ACTUAL_MW"] - merged["FORECAST_MW"]
    merged["ABS_ERROR_MW"] = merged["ERROR_MW"].abs()
    merged["SQUARE_ERROR"] = merged["ERROR_MW"] ** 2

    rmse = np.sqrt(merged["SQUARE_ERROR"].mean())
    mae = merged["ABS_ERROR_MW"].mean()
    mbe = merged["ERROR_MW"].mean()
    nrmse_percent = (rmse / avc) * 100

    summary = {
        "BLOCKS_MATCHED": len(merged),
        "AVC_MW": avc,
        "RMSE_MW": rmse,
        "NRMSE_PERCENT": nrmse_percent,
        "MAE_MW": mae,
        "MBE_MW": mbe,
        "TOTAL_ACTUAL_MW": merged["ACTUAL_MW"].sum(),
        "TOTAL_FORECAST_MW": merged["FORECAST_MW"].sum(),
    }

    return merged, summary


# ============================================================
# PRINT OUTPUT
# ============================================================

def print_terminal_output(dss_ids, avc, actual_df, actual_agg, merged, summary):
    print("\n" + "=" * 110)
    print("DSS IDS USED")
    print("=" * 110)

    for dss in dss_ids:
        print(dss)

    print("\nTotal DSS selected:", len(dss_ids))

    print("\n" + "=" * 110)
    print("AVC USED")
    print("=" * 110)
    print(f"Fixed AVC = {avc:.4f} MW")

    print("\n" + "=" * 110)
    print("ACTUAL DATA FETCHED")
    print("=" * 110)

    print(f"Actual raw rows fetched       : {len(actual_df)}")
    print(f"Actual aggregate blocks       : {len(actual_agg)}")
    print(f"Forecast vs Actual matched    : {len(merged)}")

    print("\nActual rows by DSS_ID:")
    actual_count = (
        actual_df
        .groupby("DSS_ID")
        .size()
        .reset_index(name="ROWS")
        .sort_values("DSS_ID")
    )
    print(actual_count.to_string(index=False))

    print("\n" + "=" * 110)
    print("NRMSE SUMMARY")
    print("=" * 110)

    print(f"Blocks Matched        : {summary['BLOCKS_MATCHED']}")
    print(f"AVC MW                : {summary['AVC_MW']:.4f}")
    print(f"RMSE MW               : {summary['RMSE_MW']:.4f}")
    print(f"NRMSE %               : {summary['NRMSE_PERCENT']:.4f}")
    print(f"MAE MW                : {summary['MAE_MW']:.4f}")
    print(f"MBE MW                : {summary['MBE_MW']:.4f}")
    print(f"Total Actual MW       : {summary['TOTAL_ACTUAL_MW']:.4f}")
    print(f"Total Forecast MW     : {summary['TOTAL_FORECAST_MW']:.4f}")

    print("\nFormula:")
    print("NRMSE % = SQRT(AVERAGE((Actual MW - Forecast MW)^2)) / 776.61 * 100")

    print("\n" + "=" * 110)
    print("BLOCKWISE RESULT")
    print("=" * 110)

    display_df = merged.copy()
    display_df["TIMESTAMP"] = display_df["TIMESTAMP"].dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    print(
        display_df[
            [
                "TIMESTAMP",
                "ACTUAL_MW",
                "FORECAST_MW",
                "ERROR_MW",
                "ABS_ERROR_MW",
                "SQUARE_ERROR",
            ]
        ].to_string(index=False)
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print("Loading forecast Excel...")
    print("Excel file:", FORECAST_EXCEL)

    forecast_df, dss_ids, start_time, end_time = load_forecast_excel()

    print("\nFORCED forecast/actual date:")
    print(start_time, "to", end_time)

    print("\nTotal forecast blocks:", len(forecast_df))
    print("Total DSS columns selected:", len(dss_ids))

    print(f"\nUsing fixed AVC: {FIXED_AVC_MW:.4f} MW")

    print("\nFetching actual SCADA from DSS_ACTUAL...")
    actual_df, actual_agg = fetch_actual_from_db(dss_ids, start_time, end_time)

    print("\nCalculating NRMSE...")
    merged, summary = calculate_nrmse(forecast_df, actual_agg, FIXED_AVC_MW)

    print_terminal_output(
        dss_ids=dss_ids,
        avc=FIXED_AVC_MW,
        actual_df=actual_df,
        actual_agg=actual_agg,
        merged=merged,
        summary=summary
    )


if __name__ == "__main__":
    main()