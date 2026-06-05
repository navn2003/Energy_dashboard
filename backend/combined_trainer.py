"""
combined_trainer.py
===================
Unified Solar Forecast Model Trainer
Trains ALL four model families in one run:

  ┌─────────────────────────────────────────────────────────────────────┐
  │  MODEL FAMILY          │ TARGET         │ GRANULARITY │ ARCHITECTURE │
  ├─────────────────────────────────────────────────────────────────────┤
  │  all_season_scada      │ SCADA_POWER_MW │ 15-min      │ XGBoost      │
  │  all_season_meter      │ METER_POWER_MW │ 15-min      │ XGBoost      │
  │  seasonal_scada        │ SCADA_POWER_MW │ 15-min      │ XGBoost +    │
  │    winter/spring/      │               │             │ StandardScaler│
  │    summer/monsoon      │               │             │              │
  │  seasonal_meter        │ METER_POWER_MW │ Daily agg   │ GBR Pipeline │
  │    winter/spring/      │               │             │ (StandardScal│
  │    summer/monsoon      │               │             │  + GBR)      │
  └─────────────────────────────────────────────────────────────────────┘

Season → Month mapping:
  Winter  : Oct(10), Nov(11), Dec(12), Jan(1)
  Spring  : Feb(2)
  Summer  : Mar(3), Apr(4), May(5)
  Monsoon : Jun(6), Jul(7), Aug(8), Sep(9)

Output directory structure:
  models/
  ├── all_season/
  │   ├── scada/   DSS000xx_*_xgb_scada.joblib
  │   └── meter/   DSS000xx_*_xgb_meter.joblib
  ├── seasonal_scada/
  │   └── DSS000xx/
  │       ├── winter/   model.joblib + scaler.joblib
  │       ├── spring/   model.joblib + scaler.joblib
  │       ├── summer/   model.joblib + scaler.joblib
  │       └── monsoon/  model.joblib + scaler.joblib
  ├── seasonal_meter/
  │   └── DSS000xx_Summer.joblib   (pipeline dict)
  │       DSS000xx_Winter.joblib
  │       ...
  └── model_registry.json          (index of all trained models)

Usage:
    python combined_trainer.py                          # train all families, all plants
    python combined_trainer.py --plant DSS00018         # single plant
    python combined_trainer.py --family all_season_scada
    python combined_trainer.py --family seasonal_scada
    python combined_trainer.py --family seasonal_meter
    python combined_trainer.py --family all_season_meter
    python combined_trainer.py --start 2022-01-01 --end 2024-12-31
    python combined_trainer.py --family seasonal_scada --plant DSS00018
"""

import argparse
import json
import logging
import sys
import warnings
from datetime import datetime
from pathlib import Path

import joblib
import mysql.connector
import numpy as np
import pandas as pd
from mysql.connector import Error
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import TimeSeriesSplit, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
import xgboost as xgb

warnings.filterwarnings("ignore")

# ── Logging ──────────────────────────────────────────────────────────────────
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
_sh  = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
try:
    _sh.stream.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass
logging.basicConfig(level=logging.INFO, handlers=[_sh])
log = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═════════════════════════════════════════════════════════════════════════════

TARGET_PLANTS = [
    "DSS00011", "DSS00015", "DSS00016", "DSS00017", "DSS00018",
    "DSS00019", "DSS00020", "DSS00021", "DSS00035", "DSS00037",
]

PLANT_NAMES = {
    "DSS00011": "Rajpimpri Solar 132kV",  "DSS00015": "Five Star MIDC 220kV",
    "DSS00016": "Karjat 132kV",           "DSS00017": "New MIDC Jalgaon 132kV",
    "DSS00018": "Mohol 132kV",            "DSS00019": "Kharda 132kV",
    "DSS00020": "Walwhan 132kV",          "DSS00021": "Karajgi 132kV",
    "DSS00035": "Mahtargaon 132kV",       "DSS00037": "Mograle 132kV",
}

SEASONS = {
    "Winter":  [10, 11, 12, 1],
    "Spring":  [2],
    "Summer":  [3, 4, 5],
    "Monsoon": [6, 7, 8, 9],
}

MONTH_TO_SEASON = {}
for s, months in SEASONS.items():
    for m in months:
        MONTH_TO_SEASON[m] = s

DB_CONFIG = {
    "host":            "65.1.28.178",
    "port":            3306,
    "user":            "energy",
    "password":        "Energy@123",
    "database":        "energy_monitor",
    "connect_timeout": 30,
}

# XGBoost params — shared by all-season and seasonal SCADA
XGB_PARAMS = {
    "n_estimators":          500,
    "max_depth":             6,
    "learning_rate":         0.05,
    "subsample":             0.85,
    "colsample_bytree":      0.85,
    "min_child_weight":      5,
    "reg_alpha":             0.1,
    "reg_lambda":            1.0,
    "objective":             "reg:squarederror",
    "tree_method":           "hist",
    "random_state":          42,
    "n_jobs":                -1,
    "early_stopping_rounds": 30,
}

# GBR params — seasonal meter daily model
GBR_PARAMS = {
    "n_estimators":   200,
    "learning_rate":  0.05,
    "max_depth":      4,
    "subsample":      0.8,
    "min_samples_leaf": 3,
    "random_state":   42,
}

# Weather columns used by 15-min models
WEATHER_COLS_15MIN = [
    "temperature_2m", "shortwave_radiation",
    "direct_normal_irradiance", "wind_speed_10m",
]

# Features used by seasonal SCADA 15-min models (colleagues' architecture)
SEASONAL_SCADA_FEATURES = [
    "ghi", "dni", "temp", "wind",
    "irr_ratio", "temp_factor", "wind_temp",
    "sin_h", "cos_h", "day",
]

# Features used by seasonal Meter daily model (train_models.py architecture)
SEASONAL_METER_FEATURES = [
    "temp_mean", "temp_max", "temp_min",
    "swr_mean", "swr_max", "swr_sum",
    "dni_mean", "dni_max",
    "wind_mean", "wind_max",
    "month", "dayofyear", "dayofweek", "week",
    "sin_doy", "cos_doy",
    "gen_lag_1", "gen_lag_2", "gen_lag_3", "gen_lag_7",
    "swr_lag_1", "swr_lag_2", "swr_lag_3", "swr_lag_7",
    "gen_roll_3", "gen_roll_7",
    "swr_roll_3", "swr_roll_7",
]


# ═════════════════════════════════════════════════════════════════════════════
# DB HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def get_connection():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        return conn
    except Error as e:
        log.error(f"MySQL connection failed: {e}")
        raise


def fetch_plant_metadata(conn) -> pd.DataFrame:
    placeholders = ", ".join(["%s"] * len(TARGET_PLANTS))
    query = f"""
        SELECT dss_id, dss_name, latitude, capacity_mw
        FROM   plant_master
        WHERE  dss_id IN ({placeholders})
        ORDER  BY FIELD(dss_id, {placeholders})
    """
    params = TARGET_PLANTS + TARGET_PLANTS
    return pd.read_sql(query, conn, params=params)


def fetch_scada_15min(conn, dss_id: str, start=None, end=None) -> pd.DataFrame:
    """Fetch SCADA_POWER_MW at 15-min resolution."""
    conds  = ["DSS_ID = %s", "SCADA_POWER_MW IS NOT NULL", "SCADA_POWER_MW >= 0"]
    params = [dss_id]
    if start: conds.append("TIMESTAMP >= %s"); params.append(start)
    if end:   conds.append("TIMESTAMP <= %s"); params.append(end)
    query = f"""
        SELECT DSS_ID AS plant_id, TIMESTAMP AS timestamp,
               SCADA_POWER_MW AS actual_mw
        FROM DSS_ACTUAL WHERE {" AND ".join(conds)} ORDER BY TIMESTAMP
    """
    df = pd.read_sql(query, conn, params=params)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["actual_mw"] = pd.to_numeric(df["actual_mw"], errors="coerce").fillna(0.0)
    return df


def fetch_meter_15min(conn, dss_id: str, start=None, end=None) -> pd.DataFrame:
    """Fetch METER_POWER_MW at 15-min resolution."""
    conds  = ["DSS_ID = %s", "METER_POWER_MW IS NOT NULL", "METER_POWER_MW >= 0"]
    params = [dss_id]
    if start: conds.append("TIMESTAMP >= %s"); params.append(start)
    if end:   conds.append("TIMESTAMP <= %s"); params.append(end)
    query = f"""
        SELECT DSS_ID AS plant_id, TIMESTAMP AS timestamp,
               METER_POWER_MW AS actual_mw
        FROM DSS_ACTUAL WHERE {" AND ".join(conds)} ORDER BY TIMESTAMP
    """
    df = pd.read_sql(query, conn, params=params)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["actual_mw"] = pd.to_numeric(df["actual_mw"], errors="coerce").fillna(0.0)
    return df


def fetch_weather_15min(conn, dss_id: str, start=None, end=None) -> pd.DataFrame:
    """Fetch 15-min weather from plant_weather_15min."""
    conds  = ["dss_id = %s"]
    params = [dss_id]
    if start: conds.append("block_time >= %s"); params.append(start)
    if end:   conds.append("block_time <= %s"); params.append(end)
    query = f"""
        SELECT block_time AS timestamp,
               shortwave_radiation AS ghi,
               direct_normal_irradiance AS dni,
               temperature_2m AS temp,
               wind_speed_10m AS wind,
               temperature_2m, shortwave_radiation,
               direct_normal_irradiance, wind_speed_10m
        FROM plant_weather_15min
        WHERE {" AND ".join(conds)} ORDER BY block_time
    """
    df = pd.read_sql(query, conn, params=params)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    for col in ["ghi", "dni", "temp", "wind",
                "temperature_2m", "shortwave_radiation",
                "direct_normal_irradiance", "wind_speed_10m"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df


# ═════════════════════════════════════════════════════════════════════════════
# FEATURE ENGINEERING — ALL-SEASON 15-MIN (your architecture)
# ═════════════════════════════════════════════════════════════════════════════

def build_allseason_features(df: pd.DataFrame, latitude: float = 18.5) -> pd.DataFrame:
    """
    Full feature set for all-season XGBoost models.
    Input df must have: timestamp, actual_mw, + WEATHER_COLS_15MIN columns.
    """
    dt = df["timestamp"]

    df["hour"]         = dt.dt.hour + dt.dt.minute / 60.0
    df["hour_sin"]     = np.sin(2 * np.pi * df["hour"] / 24)
    df["hour_cos"]     = np.cos(2 * np.pi * df["hour"] / 24)
    df["day_of_year"]  = dt.dt.dayofyear
    df["doy_sin"]      = np.sin(2 * np.pi * df["day_of_year"] / 365)
    df["doy_cos"]      = np.cos(2 * np.pi * df["day_of_year"] / 365)
    df["month"]        = dt.dt.month
    df["month_sin"]    = np.sin(2 * np.pi * df["month"] / 12)
    df["month_cos"]    = np.cos(2 * np.pi * df["month"] / 12)
    df["day_of_week"]  = dt.dt.dayofweek
    df["is_weekend"]   = (df["day_of_week"] >= 5).astype(int)
    df["quarter"]      = dt.dt.quarter
    df["week_of_year"] = dt.dt.isocalendar().week.astype(int)

    season_map = {
        1:"winter",2:"winter",3:"summer",4:"summer",5:"summer",
        6:"monsoon",7:"monsoon",8:"monsoon",9:"monsoon",
        10:"post_monsoon",11:"post_monsoon",12:"winter",
    }
    df["season"] = df["month"].map(season_map)
    df = pd.get_dummies(df, columns=["season"], prefix="season", drop_first=False)
    for s in ["season_winter","season_summer","season_monsoon","season_post_monsoon"]:
        if s not in df.columns: df[s] = 0

    lat_rad  = np.deg2rad(latitude)
    decl     = np.deg2rad(23.45 * np.sin(np.deg2rad(360/365*(df["day_of_year"]-81))))
    hour_ang = np.deg2rad(15 * (df["hour"] - 12))
    df["solar_elevation_proxy"] = (
        np.sin(lat_rad)*np.sin(decl) + np.cos(lat_rad)*np.cos(decl)*np.cos(hour_ang)
    ).clip(lower=0)

    if "shortwave_radiation" in df.columns:
        extra_rad = 1361*(1+0.033*np.cos(np.deg2rad(360*df["day_of_year"]/365)))
        df["clearness_index"] = (
            df["shortwave_radiation"]/(extra_rad*df["solar_elevation_proxy"].clip(lower=1e-3)+1e-6)
        ).clip(0,1)
    else:
        df["clearness_index"] = 0.0

    if "direct_normal_irradiance" in df.columns:
        df["beam_irradiance_proxy"] = df["direct_normal_irradiance"]*df["solar_elevation_proxy"]
    else:
        df["beam_irradiance_proxy"] = 0.0

    slots = 96
    df["lag_1slot"]    = df["actual_mw"].shift(1).fillna(0)
    df["lag_4slots"]   = df["actual_mw"].shift(4).fillna(0)
    df["lag_1day"]     = df["actual_mw"].shift(slots).fillna(0)
    df["lag_7day"]     = df["actual_mw"].shift(7*slots).fillna(0)
    df["roll_mean_4"]  = df["actual_mw"].shift(1).rolling(4,  min_periods=1).mean().fillna(0)
    df["roll_mean_96"] = df["actual_mw"].shift(1).rolling(96, min_periods=1).mean().fillna(0)
    df["roll_std_4"]   = df["actual_mw"].shift(1).rolling(4,  min_periods=1).std().fillna(0)

    if "capacity_mw" in df.columns:
        df["capacity_factor_lag"] = (
            df["lag_1day"]/df["capacity_mw"].replace(0,np.nan)
        ).fillna(0).clip(0,1)

    return df


def get_allseason_feature_cols(df: pd.DataFrame) -> list:
    exclude = {
        "timestamp","actual_mw","plant_id","dss_id","season_raw",
        "ghi","dni","temp","wind",
    }
    return [c for c in df.columns
            if c not in exclude and pd.api.types.is_numeric_dtype(df[c])]


# ═════════════════════════════════════════════════════════════════════════════
# FEATURE ENGINEERING — SEASONAL SCADA 15-MIN (colleagues' architecture)
# ═════════════════════════════════════════════════════════════════════════════

def build_seasonal_scada_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    10-feature set matching colleagues' seasonal SCADA trainers.
    Input must have: timestamp, actual_mw, ghi, dni, temp, wind.
    """
    df = df.copy().sort_values("timestamp")
    for col in ["ghi","dni"]:
        df[col] = df[col].fillna(0)
    for col in ["temp","wind"]:
        df[col] = df[col].fillna(df[col].median())

    df["hour"]  = df["timestamp"].dt.hour
    df["sin_h"] = np.sin(2*np.pi*df["hour"]/24)
    df["cos_h"] = np.cos(2*np.pi*df["hour"]/24)
    df["day"]   = df["timestamp"].dt.dayofyear

    df["irr_ratio"]   = np.where(df["ghi"] > 0, df["dni"]/df["ghi"], 0)
    df["temp_factor"] = np.where(df["temp"] > 25, 1-0.004*(df["temp"]-25), 1)
    df["wind_temp"]   = df["wind"] * df["temp"]

    df = df.replace([np.inf,-np.inf], 0)
    df = df.dropna(subset=["actual_mw"])
    return df


# ═════════════════════════════════════════════════════════════════════════════
# FEATURE ENGINEERING — SEASONAL METER DAILY (train_models.py architecture)
# ═════════════════════════════════════════════════════════════════════════════

def build_seasonal_meter_features(weather: pd.DataFrame, meter: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate 15-min data to daily and build lag/rolling features.
    Matches train_models.py exactly.
    """
    weather = weather.copy(); meter = meter.copy()
    weather["date"] = weather["timestamp"].dt.normalize()
    meter["date"]   = meter["timestamp"].dt.normalize()

    wday = (weather.groupby("date", sort=True).agg(
        temp_mean=("temp","mean"), temp_max=("temp","max"), temp_min=("temp","min"),
        swr_mean=("ghi","mean"),  swr_max=("ghi","max"),   swr_sum=("ghi","sum"),
        dni_mean=("dni","mean"),  dni_max=("dni","max"),
        wind_mean=("wind","mean"),wind_max=("wind","max"),
    ).reset_index())

    mday = (meter.groupby("date", sort=True).agg(
        generation_mwh=("actual_mw","sum"),
        peak_mw=("actual_mw","max"),
    ).reset_index())
    mday["generation_mwh"] *= 0.25  # 15-min → MWh

    df = pd.merge(wday, mday, on="date", how="inner").sort_values("date").reset_index(drop=True)
    if df.empty:
        return df

    df["month"]     = df["date"].dt.month
    df["dayofyear"] = df["date"].dt.dayofyear
    df["dayofweek"] = df["date"].dt.dayofweek
    df["week"]      = df["date"].dt.isocalendar().week.astype(int)
    df["sin_doy"]   = np.sin(2*np.pi*df["dayofyear"]/365)
    df["cos_doy"]   = np.cos(2*np.pi*df["dayofyear"]/365)

    for lag in [1,2,3,7]:
        df[f"gen_lag_{lag}"] = df["generation_mwh"].shift(lag)
        df[f"swr_lag_{lag}"] = df["swr_mean"].shift(lag)
    for win in [3,7]:
        df[f"gen_roll_{win}"] = df["generation_mwh"].shift(1).rolling(win).mean()
        df[f"swr_roll_{win}"] = df["swr_mean"].shift(1).rolling(win).mean()

    df["target"] = df["generation_mwh"].shift(-1)
    df["season"] = df["month"].map(MONTH_TO_SEASON)
    df = df.dropna().reset_index(drop=True)
    return df


# ═════════════════════════════════════════════════════════════════════════════
# TRAINER A — ALL-SEASON SCADA (your scada_trainer.py architecture)
# ═════════════════════════════════════════════════════════════════════════════

def train_allseason_scada(conn, plant_row: pd.Series, output_dir: Path,
                           start=None, end=None) -> dict:
    dss_id   = plant_row["dss_id"]
    dss_name = plant_row["dss_name"]
    latitude = float(plant_row.get("latitude") or 18.5)
    cap_mw   = float(plant_row.get("capacity_mw") or 0)

    scada_df   = fetch_scada_15min(conn, dss_id, start, end)
    weather_df = fetch_weather_15min(conn, dss_id, start, end)

    log.info(f"  [A-SCADA] {dss_id}: SCADA={len(scada_df)} rows, Weather={len(weather_df)} rows")
    if len(scada_df) < 200:
        log.warning(f"  [A-SCADA] {dss_id}: insufficient data, skipping.")
        return {}

    if not weather_df.empty:
        scada_df = pd.merge_asof(
            scada_df.sort_values("timestamp"),
            weather_df.sort_values("timestamp")[["timestamp","temperature_2m",
                "shortwave_radiation","direct_normal_irradiance","wind_speed_10m"]],
            on="timestamp", direction="nearest", tolerance=pd.Timedelta("30min"),
        )
    for col in ["temperature_2m","shortwave_radiation","direct_normal_irradiance","wind_speed_10m"]:
        if col not in scada_df.columns: scada_df[col] = 0.0
        scada_df[col] = scada_df[col].fillna(0.0)

    scada_df = scada_df.rename(columns={"actual_mw":"actual_mw_bak"})
    scada_df["actual_mw"] = scada_df["actual_mw_bak"]
    scada_df["capacity_mw"] = cap_mw
    df = build_allseason_features(scada_df.copy(), latitude=latitude)
    feature_cols = get_allseason_feature_cols(df)
    df = df.dropna(subset=feature_cols+["actual_mw"]).reset_index(drop=True)

    X, y = df[feature_cols], df["actual_mw"]
    tscv = TimeSeriesSplit(n_splits=5)
    cv_mae = []
    for fold,(tr,val) in enumerate(tscv.split(X),1):
        m = xgb.XGBRegressor(**XGB_PARAMS)
        m.fit(X.iloc[tr],y.iloc[tr],eval_set=[(X.iloc[val],y.iloc[val])],verbose=False)
        cv_mae.append(mean_absolute_error(y.iloc[val],m.predict(X.iloc[val]).clip(min=0)))
        log.info(f"    Fold {fold} MAE={cv_mae[-1]:.3f}")
    log.info(f"  [A-SCADA] {dss_id} CV MAE={np.mean(cv_mae):.3f}")

    split = int(len(df)*0.85)
    final = xgb.XGBRegressor(**XGB_PARAMS)
    final.fit(X.iloc[:split],y.iloc[:split],eval_set=[(X.iloc[split:],y.iloc[split:])],verbose=False)
    preds = final.predict(X.iloc[split:]).clip(min=0)

    metrics = dict(
        dss_id=dss_id, dss_name=dss_name, family="all_season_scada",
        target="SCADA_POWER_MW", granularity="15min",
        n_samples=len(df), n_features=len(feature_cols),
        cv_mae_mean=round(float(np.mean(cv_mae)),4),
        holdout_mae=round(float(mean_absolute_error(y.iloc[split:],preds)),4),
        holdout_rmse=round(float(np.sqrt(mean_squared_error(y.iloc[split:],preds))),4),
        holdout_r2=round(float(r2_score(y.iloc[split:],preds)),4),
        trained_at=datetime.now().isoformat(),
    )
    log.info(f"  [A-SCADA] {dss_id} Holdout MAE={metrics['holdout_mae']} R²={metrics['holdout_r2']}")

    safe = f"{dss_id}_{dss_name.replace(' ','_').replace('/','_')}_xgb_scada"
    path = output_dir / f"{safe}.joblib"
    joblib.dump({"model":final,"feature_cols":feature_cols,"dss_id":dss_id,
                 "dss_name":dss_name,"target":"SCADA_POWER_MW","latitude":latitude,
                 "capacity_mw":cap_mw,"metrics":metrics,"family":"all_season_scada"}, path)
    log.info(f"  Saved → {path}")
    return metrics


# ═════════════════════════════════════════════════════════════════════════════
# TRAINER B — ALL-SEASON METER (your meter_trainer.py architecture)
# ═════════════════════════════════════════════════════════════════════════════

def train_allseason_meter(conn, plant_row: pd.Series, output_dir: Path,
                           start=None, end=None) -> dict:
    dss_id   = plant_row["dss_id"]
    dss_name = plant_row["dss_name"]
    latitude = float(plant_row.get("latitude") or 18.5)
    cap_mw   = float(plant_row.get("capacity_mw") or 0)

    meter_df   = fetch_meter_15min(conn, dss_id, start, end)
    weather_df = fetch_weather_15min(conn, dss_id, start, end)

    log.info(f"  [A-METER] {dss_id}: Meter={len(meter_df)} rows, Weather={len(weather_df)} rows")
    if len(meter_df) < 200:
        log.warning(f"  [A-METER] {dss_id}: insufficient data, skipping.")
        return {}
    if (meter_df["actual_mw"] > 0).sum() == 0:
        log.warning(f"  [A-METER] {dss_id}: all-zero METER_POWER_MW, skipping.")
        return {}

    if not weather_df.empty:
        meter_df = pd.merge_asof(
            meter_df.sort_values("timestamp"),
            weather_df.sort_values("timestamp")[["timestamp","temperature_2m",
                "shortwave_radiation","direct_normal_irradiance","wind_speed_10m"]],
            on="timestamp", direction="nearest", tolerance=pd.Timedelta("30min"),
        )
    for col in ["temperature_2m","shortwave_radiation","direct_normal_irradiance","wind_speed_10m"]:
        if col not in meter_df.columns: meter_df[col] = 0.0
        meter_df[col] = meter_df[col].fillna(0.0)

    meter_df["capacity_mw"] = cap_mw
    df = build_allseason_features(meter_df.copy(), latitude=latitude)
    feature_cols = get_allseason_feature_cols(df)
    df = df.dropna(subset=feature_cols+["actual_mw"]).reset_index(drop=True)

    X, y = df[feature_cols], df["actual_mw"]
    tscv = TimeSeriesSplit(n_splits=5)
    cv_mae = []
    for fold,(tr,val) in enumerate(tscv.split(X),1):
        m = xgb.XGBRegressor(**XGB_PARAMS)
        m.fit(X.iloc[tr],y.iloc[tr],eval_set=[(X.iloc[val],y.iloc[val])],verbose=False)
        cv_mae.append(mean_absolute_error(y.iloc[val],m.predict(X.iloc[val]).clip(min=0)))
        log.info(f"    Fold {fold} MAE={cv_mae[-1]:.3f}")
    log.info(f"  [A-METER] {dss_id} CV MAE={np.mean(cv_mae):.3f}")

    split = int(len(df)*0.85)
    final = xgb.XGBRegressor(**XGB_PARAMS)
    final.fit(X.iloc[:split],y.iloc[:split],eval_set=[(X.iloc[split:],y.iloc[split:])],verbose=False)
    preds = final.predict(X.iloc[split:]).clip(min=0)

    metrics = dict(
        dss_id=dss_id, dss_name=dss_name, family="all_season_meter",
        target="METER_POWER_MW", granularity="15min",
        n_samples=len(df), n_features=len(feature_cols),
        cv_mae_mean=round(float(np.mean(cv_mae)),4),
        holdout_mae=round(float(mean_absolute_error(y.iloc[split:],preds)),4),
        holdout_rmse=round(float(np.sqrt(mean_squared_error(y.iloc[split:],preds))),4),
        holdout_r2=round(float(r2_score(y.iloc[split:],preds)),4),
        trained_at=datetime.now().isoformat(),
    )
    log.info(f"  [A-METER] {dss_id} Holdout MAE={metrics['holdout_mae']} R²={metrics['holdout_r2']}")

    safe = f"{dss_id}_{dss_name.replace(' ','_').replace('/','_')}_xgb_meter"
    path = output_dir / f"{safe}.joblib"
    joblib.dump({"model":final,"feature_cols":feature_cols,"dss_id":dss_id,
                 "dss_name":dss_name,"target":"METER_POWER_MW","latitude":latitude,
                 "capacity_mw":cap_mw,"metrics":metrics,"family":"all_season_meter"}, path)
    log.info(f"  Saved → {path}")
    return metrics


# ═════════════════════════════════════════════════════════════════════════════
# TRAINER C — SEASONAL SCADA 15-MIN (colleagues' architecture)
# ═════════════════════════════════════════════════════════════════════════════

def train_seasonal_scada(conn, plant_row: pd.Series, output_dir: Path,
                          start=None, end=None) -> list:
    dss_id   = plant_row["dss_id"]
    dss_name = plant_row["dss_name"]

    scada_df   = fetch_scada_15min(conn, dss_id, start, end)
    weather_df = fetch_weather_15min(conn, dss_id, start, end)

    log.info(f"  [S-SCADA] {dss_id}: SCADA={len(scada_df)}, Weather={len(weather_df)}")
    if scada_df.empty or weather_df.empty:
        log.warning(f"  [S-SCADA] {dss_id}: missing data, skipping all seasons.")
        return []

    # Merge weather + SCADA
    df = pd.merge_asof(
        scada_df.sort_values("timestamp"),
        weather_df.sort_values("timestamp")[["timestamp","ghi","dni","temp","wind"]],
        on="timestamp", direction="nearest", tolerance=pd.Timedelta("30min"),
    )
    df = build_seasonal_scada_features(df)

    results = []
    for season, months in SEASONS.items():
        min_rows = 300 if season == "Spring" else 500
        df_s = df[df["timestamp"].dt.month.isin(months)].copy()

        if len(df_s) < min_rows:
            log.warning(f"  [S-SCADA] {dss_id}/{season}: only {len(df_s)} rows (need {min_rows}), skipping.")
            continue

        X = df_s[SEASONAL_SCADA_FEATURES]
        y = df_s["actual_mw"]
        split = int(len(df_s)*0.8)

        scaler = StandardScaler()
        X_train = scaler.fit_transform(X.iloc[:split])
        X_test  = scaler.transform(X.iloc[split:])

        model = xgb.XGBRegressor(**{k:v for k,v in XGB_PARAMS.items()
                                    if k != "early_stopping_rounds"},
                                  early_stopping_rounds=None)
        model.fit(X_train, y.iloc[:split])
        preds = model.predict(X_test).clip(min=0)

        rmse = float(np.sqrt(mean_squared_error(y.iloc[split:], preds)))
        r2   = float(r2_score(y.iloc[split:], preds))
        mae  = float(mean_absolute_error(y.iloc[split:], preds))
        log.info(f"  [S-SCADA] {dss_id}/{season}: rows={len(df_s)} MAE={mae:.3f} RMSE={rmse:.3f} R²={r2:.3f}")

        # Save — matches colleagues' directory structure exactly
        path = output_dir / dss_id / season.lower()
        path.mkdir(parents=True, exist_ok=True)
        joblib.dump(model,  path / "model.joblib")
        joblib.dump(scaler, path / "scaler.joblib")

        # Also save metadata sidecar
        meta = dict(
            dss_id=dss_id, dss_name=dss_name, family="seasonal_scada",
            season=season, season_months=months,
            target="SCADA_POWER_MW", granularity="15min",
            feature_cols=SEASONAL_SCADA_FEATURES,
            n_samples=len(df_s), train_rows=split, test_rows=len(df_s)-split,
            mae=round(mae,4), rmse=round(rmse,4), r2=round(r2,4),
            trained_at=datetime.now().isoformat(),
        )
        with open(path/"metrics.json","w") as f: json.dump(meta,f,indent=2)
        log.info(f"  Saved → {path}/model.joblib + scaler.joblib")
        results.append(meta)

    return results


# ═════════════════════════════════════════════════════════════════════════════
# TRAINER D — SEASONAL METER DAILY (train_models.py architecture)
# ═════════════════════════════════════════════════════════════════════════════

def train_seasonal_meter(conn, plant_row: pd.Series, output_dir: Path,
                          start=None, end=None) -> list:
    dss_id   = plant_row["dss_id"]
    dss_name = plant_row["dss_name"]

    meter_df   = fetch_meter_15min(conn, dss_id, start, end)
    weather_df = fetch_weather_15min(conn, dss_id, start, end)

    log.info(f"  [S-METER] {dss_id}: Meter={len(meter_df)}, Weather={len(weather_df)}")
    if meter_df.empty or weather_df.empty:
        log.warning(f"  [S-METER] {dss_id}: missing data, skipping.")
        return []

    df = build_seasonal_meter_features(weather_df, meter_df)
    if df.empty:
        log.warning(f"  [S-METER] {dss_id}: feature build returned empty, skipping.")
        return []

    log.info(f"  [S-METER] {dss_id}: Daily feature rows={len(df)}")

    results = []
    for season, months in SEASONS.items():
        df_s = df[df["season"] == season].copy()

        if len(df_s) < 20:
            log.warning(f"  [S-METER] {dss_id}/{season}: only {len(df_s)} daily rows, skipping.")
            continue

        X = df_s[SEASONAL_METER_FEATURES]
        y = df_s["target"]

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.15, shuffle=False
        )

        pipeline = Pipeline([
            ("scaler", StandardScaler()),
            ("model",  GradientBoostingRegressor(**GBR_PARAMS)),
        ])
        pipeline.fit(X_train, y_train)
        preds = pipeline.predict(X_test)

        mae  = float(mean_absolute_error(y_test, preds))
        rmse = float(np.sqrt(mean_squared_error(y_test, preds)))
        r2   = float(r2_score(y_test, preds))
        log.info(f"  [S-METER] {dss_id}/{season}: rows={len(df_s)} MAE={mae:.3f} RMSE={rmse:.3f} R²={r2:.3f}")

        metrics = dict(
            dss_id=dss_id, dss_name=dss_name, family="seasonal_meter",
            season=season, season_months=months,
            target="METER_POWER_MW", granularity="daily",
            feature_cols=SEASONAL_METER_FEATURES,
            n_samples=len(df_s), train_rows=len(X_train), test_rows=len(X_test),
            mae_mwh=round(mae,4), rmse_mwh=round(rmse,4), r2=round(r2,4),
            trained_at=datetime.now().isoformat(),
        )

        # Save — matches train_models.py naming: DSS00018_Summer.joblib
        fname = output_dir / f"{dss_id}_{season}.joblib"
        joblib.dump({"pipeline":pipeline,"metrics":metrics}, fname, compress=3)
        log.info(f"  Saved → {fname}")
        results.append(metrics)

    return results


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

FAMILY_MAP = {
    "all_season_scada": train_allseason_scada,
    "all_season_meter": train_allseason_meter,
    "seasonal_scada":   train_seasonal_scada,
    "seasonal_meter":   train_seasonal_meter,
}

ALL_FAMILIES = list(FAMILY_MAP.keys())


def main():
    parser = argparse.ArgumentParser(
        description="Combined Solar Forecast Trainer — all four model families in one run."
    )
    parser.add_argument("--plant",  default=None,
        help="Single DSS_ID. Omit to train all 10 plants.")
    parser.add_argument("--family", default=None,
        choices=ALL_FAMILIES,
        help=f"Train only one family. Omit to train all. Choices: {ALL_FAMILIES}")
    parser.add_argument("--start",  default=None, help="Training start date YYYY-MM-DD.")
    parser.add_argument("--end",    default=None, help="Training end date YYYY-MM-DD.")
    parser.add_argument("--output", default="models",
        help="Root output directory (default: models/). Subdirs created automatically.")
    args = parser.parse_args()

    # Plants
    if args.plant:
        if args.plant not in TARGET_PLANTS:
            raise ValueError(f"'{args.plant}' not in TARGET_PLANTS.\nValid: {TARGET_PLANTS}")
        plants_to_run = [args.plant]
    else:
        plants_to_run = TARGET_PLANTS

    # Families
    families_to_run = [args.family] if args.family else ALL_FAMILIES

    # Output dirs
    root = Path(args.output)
    dirs = {
        "all_season_scada": root / "all_season" / "scada",
        "all_season_meter": root / "all_season" / "meter",
        "seasonal_scada":   root / "seasonal_scada",
        "seasonal_meter":   root / "seasonal_meter",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    log.info("=" * 65)
    log.info("Combined Solar Forecast Trainer")
    log.info(f"Plants  : {plants_to_run}")
    log.info(f"Families: {families_to_run}")
    log.info("=" * 65)

    conn = get_connection()
    try:
        plants_meta = fetch_plant_metadata(conn)
        plants_meta = plants_meta[plants_meta["dss_id"].isin(plants_to_run)]

        all_metrics = []

        for _, plant_row in plants_meta.iterrows():
            dss_id = plant_row["dss_id"]
            log.info(f"\n{'='*65}")
            log.info(f"PLANT: {dss_id} — {plant_row['dss_name']}")
            log.info(f"{'='*65}")

            for family in families_to_run:
                log.info(f"\n  --- {family.upper()} ---")
                trainer = FAMILY_MAP[family]
                out_dir = dirs[family]
                try:
                    result = trainer(conn, plant_row, out_dir, args.start, args.end)
                    if isinstance(result, dict) and result:
                        result["family"] = family
                        all_metrics.append(result)
                    elif isinstance(result, list):
                        all_metrics.extend(result)
                except Exception as e:
                    log.error(f"  [{family}] {dss_id} FAILED: {e}")

        # Save unified registry
        registry_path = root / "model_registry.json"
        with open(registry_path, "w") as f:
            json.dump(all_metrics, f, indent=2)
        log.info(f"\nModel registry saved → {registry_path}")

        # Print summary
        if all_metrics:
            df_sum = pd.DataFrame(all_metrics)
            cols = [c for c in ["dss_id","family","season","target","granularity",
                                 "n_samples","holdout_mae","mae","mae_mwh",
                                 "holdout_r2","r2"] if c in df_sum.columns]
            log.info("\n" + "=" * 65 + "\n  TRAINING SUMMARY\n" + "=" * 65)
            log.info("\n" + df_sum[cols].to_string(index=False))

            # Per-family CSV summaries
            for fam in families_to_run:
                sub = df_sum[df_sum.get("family","") == fam] if "family" in df_sum.columns else pd.DataFrame()
                if not sub.empty:
                    sub.to_csv(dirs[fam] / "training_summary.csv", index=False)

    finally:
        conn.close()
        log.info("\nDB connection closed.")


if __name__ == "__main__":
    main()
