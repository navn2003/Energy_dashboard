from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, Response
from database import get_connection
from datetime import date, timedelta
import calendar
import pandas as pd
import os
import re
import pymysql
import subprocess
import uuid
from pathlib import Path
from typing import Optional, Dict, Any
from pydantic import BaseModel
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

app = FastAPI(title="Energy Monitor API")
# ... (rest of your code)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# ── Authentication (paste into main.py AFTER `app = FastAPI(...)` and any CORS  ──
#    setup, and BEFORE/around your other routes — anywhere `app` already exists). ─
import os, hmac, hashlib, base64, time
from fastapi import Request
from pydantic import BaseModel

# Credentials & secret — OVERRIDE THESE VIA ENV (esp. on public Replit!).
AUTH_USER   = os.getenv("AUTH_USER", "Admin")
AUTH_PASS   = os.getenv("AUTH_PASS", "Energy@123")
AUTH_SECRET = os.getenv("AUTH_SECRET", "please-change-this-long-random-secret-7f3a9c21")
AUTH_TTL    = int(os.getenv("AUTH_TTL", "43200"))   # token lifetime in seconds (12h)

# /api/ paths that do NOT require a token:
_PUBLIC_API = {"/api/login"}


def _make_token(username: str) -> str:
    exp = int(time.time()) + AUTH_TTL
    msg = f"{username}:{exp}"
    sig = hmac.new(AUTH_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}:{sig}".encode()).decode()


def _verify_token(token: str):
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, exp, sig = raw.rsplit(":", 2)
        if int(exp) < time.time():
            return None
        expected = hmac.new(AUTH_SECRET.encode(), f"{username}:{exp}".encode(),
                            hashlib.sha256).hexdigest()
        return username if hmac.compare_digest(sig, expected) else None
    except Exception:
        return None


class _LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def api_login(body: _LoginIn):
    if hmac.compare_digest(body.username, AUTH_USER) and hmac.compare_digest(body.password, AUTH_PASS):
        return {"token": _make_token(body.username), "username": body.username}
    return JSONResponse({"error": "Invalid username or password."}, status_code=401)


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    path = request.url.path
    # Only guard the API; static pages stay reachable so the login screen can load.
    if path.startswith("/api/") and path not in _PUBLIC_API and request.method != "OPTIONS":
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not _verify_token(token):
            return JSONResponse({"error": "Unauthorized", "auth_required": True},
                                status_code=401)
    return await call_next(request)
# ── NRMSE endpoints ──────────────────────────────────────────────────────────

@app.get("/api/nrmse/daily")
def get_nrmse_daily(date: str = Query(..., description="YYYY-MM-DD")):
    """Returns NRMSE values for a single day grouped by company, energy_type, revision_num."""
    sql = """
        SELECT company, energy_type, revision_num, nrmse_values
        FROM daily_nrmse_values
        WHERE date = %s
        ORDER BY energy_type, company, revision_num
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql, (date,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    return _build_nrmse_response(rows)


@app.get("/api/nrmse/monthly")
def get_nrmse_monthly(year: int = Query(...), month: int = Query(...)):
    """Returns precomputed monthly NRMSE values from monthly_nrmse_values."""
    sql = """
        SELECT company, energy_type, revision_num, nrmse_values
        FROM monthly_nrmse_values
        WHERE YEAR(`month`) = %s AND MONTH(`month`) = %s
        ORDER BY energy_type, company, revision_num
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql, (year, month))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    return _build_monthly_nrmse_response(rows)


def _build_monthly_nrmse_response(rows):
    """
    Builder used ONLY by the monthly endpoint (monthly_nrmse_values table).
    ISPL:    R20 → R16,  R02 → DA2
    Quenext: R-16 → R16, RD2 → DA2
    """
    MONTHLY_REV_MAP = {
        "R20": "R16",
        "R02": "DA2",
        "R-16": "R16",
        "RD2":  "DA2",
    }

    result = {"solar": {}, "wind": {}}

    for row in rows:
        company     = row["company"]
        energy_type = row["energy_type"].lower()
        rev_raw     = row["revision_num"]
        nrmse_val   = float(row["nrmse_values"]) if row["nrmse_values"] is not None else None
        rev_label   = MONTHLY_REV_MAP.get(rev_raw, rev_raw)

        if energy_type not in result:
            result[energy_type] = {}
        if company not in result[energy_type]:
            result[energy_type][company] = {}

        result[energy_type][company][rev_label] = nrmse_val

    return result

@app.get("/api/nrmse/custom")
def get_nrmse_custom(
    from_date: str = Query(..., description="YYYY-MM-DD"),
    to_date: str = Query(..., description="YYYY-MM-DD"),
):
    """Returns average NRMSE values for a custom date range."""
    sql = """
        SELECT company, energy_type, revision_num,
        AVG(nrmse_values) AS nrmse_values
        FROM daily_nrmse_values
        WHERE date BETWEEN %s AND %s
        GROUP BY company, energy_type, revision_num
        ORDER BY energy_type, company, revision_num
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql, (from_date, to_date))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    return _build_nrmse_response(rows)


def _build_nrmse_response(rows):
    """
    Transforms flat DB rows into a structured dict.
    ISPL:    R20 → R16,  R02 → DA2
    Quenext: R-16 → R16, Rd2 → DA2   (adjust these to match your actual DB values)
    """
    REV_MAP = {
        # ISPL revision codes
        "R20": "R16",
        "R02": "DA2",
        # Quenext revision codes
        "R-16": "R16",
        "RD2":  "DA2",
        # Add any other variants you see in the DB:
        # "R16":  "R16",
        # "DA2":  "DA2",
    }

    result = {
        "solar": {},
        "wind": {},
    }

    for row in rows:
        company     = row["company"]
        energy_type = row["energy_type"].lower()
        rev_raw     = row["revision_num"]
        nrmse_val   = float(row["nrmse_values"]) if row["nrmse_values"] is not None else None
        rev_label   = REV_MAP.get(rev_raw, rev_raw)   # falls back to raw value if unmapped

        if energy_type not in result:
            result[energy_type] = {}
        if company not in result[energy_type]:
            result[energy_type][company] = {}

        result[energy_type][company][rev_label] = nrmse_val

    return result


# ── NRMSE Monitor (per-day green/yellow/red breakdown) ────────────────────────

# Same revision mapping as _build_nrmse_response, but we expose DA2 under the
# label "R02" because that is the column header requested in the Monitor UI.
_MONITOR_REV_MAP = {
    "R20": "R16",   # ISPL intra-day
    "R-16": "R16",  # Quenext intra-day
    "R02": "R02",   # ISPL day-ahead
    "RD2": "R02",   # Quenext day-ahead
    "DA2": "R02",
    "R16": "R16",
}

# Thresholds [green_max, yellow_max] per energy type and logical revision.
# Matches nrmse_live.html: solar R16=[3,5] DA2=[5,10]; wind R16=[5,10] DA2=[10,15].
_MONITOR_THRESHOLDS = {
    "SOLAR": {"R16": [3, 5],  "R02": [5, 10]},
    "WIND":  {"R16": [5, 10], "R02": [10, 15]},
}


def _classify(value, lo, hi):
    """green if <=lo, yellow if <=hi, else red. None -> None."""
    if value is None:
        return None
    v = float(value)
    if v <= lo:
        return "green"
    if v <= hi:
        return "yellow"
    return "red"


def _monitor_payload(year: int, month: int, energy_type: str):
    energy = (energy_type or "SOLAR").upper()
    thresholds = _MONITOR_THRESHOLDS.get(energy, _MONITOR_THRESHOLDS["SOLAR"])

    sql = """
        SELECT DAY(date) AS day, company, revision_num, nrmse_values
        FROM daily_nrmse_values
        WHERE YEAR(date) = %s AND MONTH(date) = %s AND UPPER(energy_type) = %s
        ORDER BY day
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(sql, (year, month, energy))
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    days_in_month = calendar.monthrange(year, month)[1]

    def blank_company():
        days = {
            d: {"R16": {"value": None, "status": None},
                "R02": {"value": None, "status": None}}
            for d in range(1, days_in_month + 1)
        }
        counts = {"R16": {"green": 0, "yellow": 0, "red": 0},
                  "R02": {"green": 0, "yellow": 0, "red": 0}}
        return {"days": days, "counts": counts}

    companies: Dict[str, Any] = {"ISPL": blank_company(), "Quenext": blank_company()}

    for row in rows:
        raw_company = str(row["company"] or "").strip().upper()
        if "QUENEXT" in raw_company:
            comp = "Quenext"
        elif "ISPL" in raw_company:
            comp = "ISPL"
        else:
            continue

        rev = _MONITOR_REV_MAP.get(str(row["revision_num"]).strip())
        if rev not in ("R16", "R02"):
            continue

        day = int(row["day"])
        val = float(row["nrmse_values"]) if row["nrmse_values"] is not None else None
        lo, hi = thresholds[rev]
        status = _classify(val, lo, hi)

        cell = companies[comp]["days"].get(day)
        if cell is None:
            continue
        cell[rev] = {"value": round(val, 4) if val is not None else None, "status": status}
        if status:
            companies[comp]["counts"][rev][status] += 1

    # Convert day dicts to ordered lists for easy table rendering.
    for comp in companies:
        day_map = companies[comp]["days"]
        companies[comp]["days"] = [
            {"date": d, "R16": day_map[d]["R16"], "R02": day_map[d]["R02"]}
            for d in range(1, days_in_month + 1)
        ]

    return {
        "year": year,
        "month": month,
        "energy_type": energy,
        "days_in_month": days_in_month,
        "thresholds": thresholds,
        "companies": companies,
    }


@app.get("/api/nrmse/monitor")
def get_nrmse_monitor(
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    energy_type: str = Query("SOLAR", description="SOLAR or WIND"),
):
    """Per-day green/yellow/red classification for ISPL & Quenext, revisions R16 & R02."""
    return _monitor_payload(year, month, energy_type)


@app.get("/api/nrmse/monitor-csv")
def get_nrmse_monitor_csv(
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    energy_type: str = Query("SOLAR"),
):
    """CSV export of the per-day NRMSE classification."""
    data = _monitor_payload(year, month, energy_type)

    lines = ["Company,Date,R16_Value,R16_Status,R02_Value,R02_Status"]
    for comp, cdata in data["companies"].items():
        for d in cdata["days"]:
            r16, r02 = d["R16"], d["R02"]
            date_str = f"{year:04d}-{month:02d}-{d['date']:02d}"
            lines.append(",".join([
                comp,
                date_str,
                "" if r16["value"] is None else str(r16["value"]),
                r16["status"] or "",
                "" if r02["value"] is None else str(r02["value"]),
                r02["status"] or "",
            ]))

    csv_text = "\n".join(lines)
    fname = f"nrmse_monitor_{data['energy_type']}_{year}_{month:02d}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── Plant endpoints ──────────────────────────────────────────────────────────

@app.get("/api/plants")
def get_plants():
    """Returns all plants from plant_master."""
    sql = """
        SELECT plant_id, plant_name, plant_type, capacity_mw
        FROM plant_master
        ORDER BY plant_type, plant_name
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
    except Exception as e:
        cursor.close()
        conn.close()
        return {"error": str(e), "plants": []}
    cursor.close()
    conn.close()
    return {"plants": rows}


# ── Chart data (96 slots) ─────────────────────────────────────────────────────

@app.get("/api/chart/daily")
def get_chart_daily(
    date: str = Query(..., description="YYYY-MM-DD"),
    plant_id: str = Query(None, description="Optional plant filter"),
    energy_type: str = Query(None, description="SOLAR or WIND"),
    revision: str = Query("R16", description="Logical revision: R16 (intra-day) or DA2 (day-ahead)"),
):
    """
    Returns 96-slot (15-min) forecast vs actual data for charting.
    Pulls strictly from dss_forecast_aggregated.

    Each vendor stores the same logical revision under a different raw code:
        Logical R16 (intra-day):  ISPL='R20',  Quenext='R-16'
        Logical DA2 (day-ahead):  ISPL='R02',  Quenext='Rd2'
    So we match per-vendor instead of a single revision string.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # 1. Initialize the 96 slots perfectly formatted to HH:MM
    slots = _generate_slots()
    result_map = {slot: {"slot": slot, "actual": None} for slot in slots}

    # 2. Resolve the per-vendor raw revision codes for the requested logical revision.
    rev_logical = (revision or "R16").upper().replace("-", "").replace(" ", "")
    REVISION_MAP = {
        "R16": {"ISPL": "R20",  "Quenext": "R-16"},   # intra-day
        "DA2": {"ISPL": "R02",  "Quenext": "Rd2"},     # day-ahead
    }
    vendor_codes = REVISION_MAP.get(rev_logical, REVISION_MAP["R16"])
    ispl_rev = vendor_codes["ISPL"]
    quen_rev = vendor_codes["Quenext"]

    # 3. Match each vendor against its own revision code (case-insensitive).
    sql = """
        SELECT
            DATE_FORMAT(timestamp, '%H:%i') AS time_slot,
            company_name,
            revision_number,
            total_forecast_mw,
            total_scada
        FROM dss_forecast_aggregated
        WHERE DATE(timestamp) = %s
          AND UPPER(energy_type) = %s
          AND (
                (UPPER(company_name) LIKE '%%ISPL%%'    AND UPPER(revision_number) = %s)
             OR (UPPER(company_name) LIKE '%%QUENEXT%%' AND UPPER(revision_number) = %s)
          )
    """

    try:
        cursor.execute(sql, (
            date,
            (energy_type or "").upper(),
            ispl_rev.upper(),
            quen_rev.upper(),
        ))
        rows = cursor.fetchall()
    except Exception as e:
        print(f"Chart query error: {e}")
        rows = []
    finally:
        cursor.close()
        conn.close()

    # 4. Map values strictly to frontend format
    for row in rows:
        slot_key = row["time_slot"]

        if slot_key in result_map:
            # Canonicalise company name to the exact keys the UI reads
            # (frontend expects s.ISPL and s.Quenext).
            raw = str(row["company_name"] or "").strip().upper()
            if "QUENEXT" in raw or raw.startswith("QUE"):
                company = "Quenext"
            elif "ISPL" in raw:
                company = "ISPL"
            else:
                company = raw  # fallback: store under raw name

            # Map Forecast
            if row["total_forecast_mw"] is not None:
                result_map[slot_key][company] = float(row["total_forecast_mw"])

            # Map Actual SCADA (same SCADA value for both vendors; keep first non-null)
            if row["total_scada"] is not None and result_map[slot_key]["actual"] is None:
                result_map[slot_key]["actual"] = float(row["total_scada"])


    return {"slots": list(result_map.values())}


def _generate_slots():
    """Generates 96 time slots: 00:00, 00:15, ... 23:45"""
    slots = []
    for h in range(24):
        for m in (0, 15, 30, 45):
            slots.append(f"{h:02d}:{m:02d}")
    return slots


# ── Available dates ───────────────────────────────────────────────────────────

@app.get("/api/available-dates")
def get_available_dates():
    """Returns distinct dates that have NRMSE data."""
    sql = "SELECT DISTINCT date FROM daily_nrmse_values ORDER BY date DESC LIMIT 90"
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return {"dates": [str(r[0]) for r in rows]}


@app.get("/api/health")
def health():
    return {"status": "ok"}
# =============================================================================
# ── SCADA REPORTS INTEGRATION ────────────────────────────────────────────────
# =============================================================================

SCADA_DB_CONFIG = {
    "host": "13.205.184.74",
    "user": "normal_access",
    "password": "energyX@123#",
    "database": "engxai_fs",
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}

def get_scada_connection():
    return pymysql.connect(**SCADA_DB_CONFIG)

def clean_dss_id(value):
    if value is None: return None
    value = str(value).strip()
    if value == "" or value.upper() == "DSS_ID": return None
    if not re.match(r"^DSS\d+$", value): return None
    return value

def get_all_plants_scada(conn):
    plants = []
    with conn.cursor() as cur:
        cur.execute("SELECT DSS_ID FROM DSS_MASTER WHERE DSS_ID IS NOT NULL AND DSS_ID <> '' ORDER BY DSS_ID")
        rows = cur.fetchall()
    for row in rows:
        dss = clean_dss_id(row.get("DSS_ID"))
        if dss: plants.append(dss)
    return sorted(list(set(plants)))

def get_scada_lookup(conn, date):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DSS_ID, TIMESTAMP, SCADA_POWER_MW FROM DSS_ACTUAL
            WHERE TIMESTAMP >= %s AND TIMESTAMP < DATE_ADD(%s, INTERVAL 1 DAY)
        """, (date, date))
        data_rows = cur.fetchall()
    lookup = {}
    for row in data_rows:
        dss = clean_dss_id(row.get("DSS_ID"))
        if not dss: continue
        ts = row.get("TIMESTAMP")
        if ts is None: continue
        ts = pd.to_datetime(ts, errors="coerce")
        if pd.isna(ts): continue
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
        scada_number = pd.to_numeric(row.get("SCADA_POWER_MW"), errors="coerce")
        key = (ts_str, dss)
        lookup[key] = "FULL" if pd.notna(scada_number) else "PARTIAL"
    return lookup, len(data_rows)

def get_monthly_scada_lookup(conn, month):
    start_ts = pd.to_datetime(f"{month}-01")
    end_ts = start_ts + pd.offsets.MonthBegin(1)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DSS_ID, TIMESTAMP, SCADA_POWER_MW FROM DSS_ACTUAL
            WHERE TIMESTAMP >= %s AND TIMESTAMP < %s
        """, (start_ts.strftime("%Y-%m-%d"), end_ts.strftime("%Y-%m-%d")))
        data_rows = cur.fetchall()
    lookup = {}
    for row in data_rows:
        dss = clean_dss_id(row.get("DSS_ID"))
        if not dss: continue
        ts = pd.to_datetime(row.get("TIMESTAMP"), errors="coerce")
        if pd.isna(ts): continue
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
        scada_number = pd.to_numeric(row.get("SCADA_POWER_MW"), errors="coerce")
        lookup[(ts_str, dss)] = "FULL" if pd.notna(scada_number) else "PARTIAL"
    return lookup, len(data_rows), start_ts, end_ts

@app.get("/api/report")
def api_report(date: str = Query(...)):
    try:
        conn = get_scada_connection()
        plants = get_all_plants_scada(conn)
        time_slots = pd.date_range(start=f"{date} 00:00:00", end=f"{date} 23:55:00", freq="5min")
        lookup, total_rows = get_scada_lookup(conn, date)
        conn.close()
        
        matrix = {}
        for ts in time_slots:
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            matrix[ts_str] = {plant: lookup.get((ts_str, plant), "NO DATA") for plant in plants}
            
        return {"date": date, "plants": plants, "timestamps": list(matrix.keys()), "total_db_rows": total_rows, "matrix": matrix}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/missing-report")
def missing_report(date: str = Query(...)):
    try:
        conn = get_scada_connection()
        plants = get_all_plants_scada(conn)
        time_slots = pd.date_range(start=f"{date} 00:00:00", end=f"{date} 23:55:00", freq="5min")
        total_slots = len(time_slots)
        lookup, total_rows = get_scada_lookup(conn, date)
        conn.close()

        report, total_full, total_partial, total_missing = [], 0, 0, 0
        for plant in plants:
            full, partial, missing, missing_ts = 0, 0, 0, []
            for ts in time_slots:
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
                status = lookup.get((ts_str, plant), "NO DATA")
                if status == "FULL": full += 1
                elif status == "PARTIAL": partial += 1
                else:
                    missing += 1
                    missing_ts.append(ts_str)
                    
            total_full += full; total_partial += partial; total_missing += missing
            report.append({
                "DSS_ID": plant, "TOTAL_SLOTS": total_slots, "FULL_COUNT": full,
                "PARTIAL_COUNT": partial, "MISSING_COUNT": missing,
                "AVAILABILITY_PERCENT": round((full / total_slots) * 100, 2),
                "MISSING_PERCENT": round((missing / total_slots) * 100, 2),
                "MISSING_TIMESTAMPS": missing_ts
            })
            
        return {"date": date, "total_plants": len(plants), "total_slots": total_slots, "total_db_rows": total_rows, "summary": {"TOTAL_FULL": total_full, "TOTAL_PARTIAL": total_partial, "TOTAL_MISSING": total_missing}, "report": report}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/monthly-missing-report")
def monthly_missing_report(month: str = Query(...)):
    try:
        conn = get_scada_connection()
        plants = get_all_plants_scada(conn)
        lookup, total_rows, start_ts, end_ts = get_monthly_scada_lookup(conn, month)
        conn.close()

        time_slots = pd.date_range(start=start_ts.strftime("%Y-%m-%d 00:00:00"), end=(end_ts - pd.Timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"), freq="5min")
        total_slots = len(time_slots)
        report, total_full, total_partial, total_missing = [], 0, 0, 0

        for plant in plants:
            full, partial, missing, missing_days = 0, 0, 0, {}
            for ts in time_slots:
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
                day_str = ts.strftime("%Y-%m-%d")
                status = lookup.get((ts_str, plant), "NO DATA")
                if status == "FULL": full += 1
                elif status == "PARTIAL": partial += 1
                else:
                    missing += 1
                    missing_days[day_str] = missing_days.get(day_str, 0) + 1

            total_full += full; total_partial += partial; total_missing += missing
            report.append({
                "DSS_ID": plant, "TOTAL_SLOTS": total_slots, "FULL_COUNT": full,
                "PARTIAL_COUNT": partial, "MISSING_COUNT": missing,
                "AVAILABILITY_PERCENT": round((full / total_slots) * 100, 2),
                "MISSING_PERCENT": round((missing / total_slots) * 100, 2),
                "MISSING_DAYS": missing_days
            })

        return {"month": month, "start_date": start_ts.strftime("%Y-%m-%d"), "end_date": (end_ts - pd.Timedelta(days=1)).strftime("%Y-%m-%d"), "total_plants": len(plants), "total_slots": total_slots, "total_db_rows": total_rows, "summary": {"TOTAL_FULL": total_full, "TOTAL_PARTIAL": total_partial, "TOTAL_MISSING": total_missing}, "report": report}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/missing-report-pdf")
def missing_report_pdf(date: str = Query(...)):
    # Re-using logic to generate PDF
    try:
        conn = get_scada_connection()
        plants = get_all_plants_scada(conn)
        time_slots = pd.date_range(start=f"{date} 00:00:00", end=f"{date} 23:55:00", freq="5min")
        total_slots = len(time_slots)
        lookup, total_rows = get_scada_lookup(conn, date)
        conn.close()

        pdf_file = f"missing_scada_report_{date}.pdf"
        pdf_path = os.path.join(os.path.dirname(__file__), pdf_file)
        doc = SimpleDocTemplate(pdf_path, pagesize=landscape(A4), rightMargin=20, leftMargin=20, topMargin=20, bottomMargin=20)
        styles = getSampleStyleSheet()
        elements = [Paragraph(f"SCADA Missing Data Report - {date}", styles["Title"]), Spacer(1, 12)]
        elements.append(Paragraph(f"Date: {date}<br/>Total Plants: {len(plants)}<br/>Total Slots per Plant: {total_slots}<br/>Total DB Rows: {total_rows}", styles["Normal"]))
        elements.append(Spacer(1, 12))

        table_data = [["DSS_ID", "Total Slots", "Full", "Partial", "Missing", "Availability %", "Missing %", "Missing Times"]]
        for plant in plants:
            full, partial, missing, missing_times = 0, 0, 0, []
            for ts in time_slots:
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
                status = lookup.get((ts_str, plant), "NO DATA")
                if status == "FULL": full += 1
                elif status == "PARTIAL": partial += 1
                else:
                    missing += 1
                    missing_times.append(ts.strftime("%H:%M"))
            
            missing_text = ", ".join(missing_times)
            if len(missing_text) > 180: missing_text = missing_text[:180] + " ..."
            table_data.append([plant, total_slots, full, partial, missing, f"{round((full/total_slots)*100,2)}%", f"{round((missing/total_slots)*100,2)}%", missing_text])

        table = Table(table_data, repeatRows=1, colWidths=[70, 65, 45, 55, 60, 75, 65, 360])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222222")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("BACKGROUND", (2, 1), (2, -1), colors.HexColor("#c8e6c9")),
            ("BACKGROUND", (3, 1), (3, -1), colors.HexColor("#fff9c4")), ("BACKGROUND", (4, 1), (4, -1), colors.HexColor("#ffcdd2")),
        ]))
        elements.append(table)
        doc.build(elements)
        return FileResponse(path=pdf_path, filename=pdf_file, media_type="application/pdf")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
@app.get("/api/missing-report-csv")
def missing_report_csv(date: str = Query(...)):
    try:
        conn = get_scada_connection()
        plants = get_all_plants_scada(conn)
        time_slots = pd.date_range(start=f"{date} 00:00:00", end=f"{date} 23:55:00", freq="5min")
        total_slots = len(time_slots)
        lookup, total_rows = get_scada_lookup(conn, date)
        conn.close()

        # Define CSV headers
        lines = ["DSS_ID,Total Slots,Full Count,Partial Count,Missing Count,Availability %,Missing %,Missing Timestamps"]
        
        for plant in plants:
            full, partial, missing, missing_times = 0, 0, 0, []
            for ts in time_slots:
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
                status = lookup.get((ts_str, plant), "NO DATA")
                if status == "FULL": 
                    full += 1
                elif status == "PARTIAL": 
                    partial += 1
                else:
                    missing += 1
                    missing_times.append(ts.strftime("%H:%M"))
            
            # Join timestamps with spaces and wrap the string in quotes to prevent CSV column splitting
            missing_text = " ".join(missing_times)
            avail_pct = round((full / total_slots) * 100, 2)
            miss_pct = round((missing / total_slots) * 100, 2)
            
            # Append the row data
            lines.append(f'{plant},{total_slots},{full},{partial},{missing},{avail_pct}%,{miss_pct}%,"{missing_text}"')

        csv_text = "\n".join(lines)
        fname = f"missing_scada_report_{date}.csv"
        
        return Response(
            content=csv_text,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
@app.get("/api/monthly-missing-report-pdf")
def monthly_missing_report_pdf(month: str = Query(...)):
    try:
        conn = get_scada_connection()
        plants = get_all_plants_scada(conn)
        lookup, total_rows, start_ts, end_ts = get_monthly_scada_lookup(conn, month)
        conn.close()

        time_slots = pd.date_range(start=start_ts.strftime("%Y-%m-%d 00:00:00"), end=(end_ts - pd.Timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"), freq="5min")
        total_slots = len(time_slots)

        pdf_file = f"monthly_missing_scada_report_{month}.pdf"
        pdf_path = os.path.join(os.path.dirname(__file__), pdf_file)
        doc = SimpleDocTemplate(pdf_path, pagesize=landscape(A4), rightMargin=20, leftMargin=20, topMargin=20, bottomMargin=20)
        styles = getSampleStyleSheet()
        elements = [Paragraph(f"Monthly SCADA Missing Data Report - {month}", styles["Title"]), Spacer(1, 12)]
        elements.append(Paragraph(f"Month: {month}<br/>From: {start_ts.strftime('%Y-%m-%d')} To {(end_ts - pd.Timedelta(days=1)).strftime('%Y-%m-%d')}<br/>Total Plants: {len(plants)}<br/>Total Slots per Plant: {total_slots}<br/>Total DB Rows: {total_rows}", styles["Normal"]))
        elements.append(Spacer(1, 12))

        table_data = [["DSS_ID", "Total Slots", "Full", "Partial", "Missing", "Availability %", "Missing %", "Missing Days"]]
        for plant in plants:
            full, partial, missing, missing_days = 0, 0, 0, {}
            for ts in time_slots:
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
                day_str = ts.strftime("%d-%m")
                status = lookup.get((ts_str, plant), "NO DATA")
                if status == "FULL": full += 1
                elif status == "PARTIAL": partial += 1
                else:
                    missing += 1
                    missing_days[day_str] = missing_days.get(day_str, 0) + 1

            missing_days_text = ", ".join([f"{day}: {count}" for day, count in missing_days.items()])
            if len(missing_days_text) > 220: missing_days_text = missing_days_text[:220] + " ..."
            table_data.append([plant, total_slots, full, partial, missing, f"{round((full/total_slots)*100,2)}%", f"{round((missing/total_slots)*100,2)}%", missing_days_text])

        table = Table(table_data, repeatRows=1, colWidths=[70, 65, 45, 55, 60, 75, 65, 360])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222222")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("BACKGROUND", (2, 1), (2, -1), colors.HexColor("#c8e6c9")),
            ("BACKGROUND", (3, 1), (3, -1), colors.HexColor("#fff9c4")), ("BACKGROUND", (4, 1), (4, -1), colors.HexColor("#ffcdd2")),
        ]))
        elements.append(table)
        doc.build(elements)
        return FileResponse(path=pdf_path, filename=pdf_file, media_type="application/pdf")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    # =============================================================================
# ── ON-DEMAND FORECASTING INTEGRATION ────────────────────────────────────────
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "forecast_outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

# Ensure this points to the correct combined_forecaster.py in your folder
FORECASTER_FILE = BASE_DIR / "combined_forecaster.py"

class ForecastRequest(BaseModel):
    plant: str = "ALL"                 
    date: str                          
    horizon: int = 96                  
    family: Optional[str] = None       
    models_dir: str = "models"
    evaluate: bool = False
    ensemble: bool = False

def _find_col(df: pd.DataFrame, *candidates: str) -> Optional[str]:
    """Find a column by case-insensitive match against a list of candidate names."""
    lower = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def _read_plant_sheet(path: Path, sheet: str) -> Optional[pd.DataFrame]:
    """Read one plant sheet and normalise to columns: timestamp, forecast_mw."""
    df = pd.read_excel(path, sheet_name=sheet)
    ts_col = _find_col(df, "timestamp", "time", "datetime", "block_time")
    mw_col = _find_col(df, "forecast_mw", "forecast", "predicted_mw", "pred_mw", "mw", "value")
    if ts_col is None or mw_col is None:
        return None
    out = df[[ts_col, mw_col]].copy()
    out.columns = ["timestamp", "forecast_mw"]
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out["forecast_mw"] = pd.to_numeric(out["forecast_mw"], errors="coerce")
    out = out.dropna(subset=["timestamp"])
    out["forecast_mw"] = out["forecast_mw"].fillna(0.0)
    return out if not out.empty else None


def _parse_excel_output(path: Path, requested_plant: str = "ALL") -> Dict[str, Any]:
    if not path.exists():
        return {"error": "Forecast finished, but Excel was not created."}

    xls = pd.ExcelFile(path)
    response: Dict[str, Any] = {"sheets": xls.sheet_names, "summary": [], "series": []}

    if "Summary" in xls.sheet_names:
        summary = pd.read_excel(path, sheet_name="Summary")
        response["summary"] = summary.fillna("").to_dict(orient="records")

    plant_sheets = [s for s in xls.sheet_names if s not in ("Summary", "Evaluation")]
    if not plant_sheets:
        response["note"] = "No plant sheets in the Excel — the model produced no forecast rows."
        return response

    # Read every plant sheet we can normalise.
    frames: Dict[str, pd.DataFrame] = {}
    for s in plant_sheets:
        df = _read_plant_sheet(path, s)
        if df is not None:
            frames[s] = df

    if not frames:
        response["note"] = "Plant sheets found but no recognisable timestamp/MW columns."
        return response

    req = (requested_plant or "ALL").strip()
    if req.upper() == "ALL" and len(frames) > 1:
        # Aggregate: total system MW per timestamp across all plants.
        combined = pd.concat(frames.values(), ignore_index=True)
        agg = (combined.groupby("timestamp", as_index=False)["forecast_mw"]
                       .sum().sort_values("timestamp"))
        chart_label = f"All Plants (sum of {len(frames)})"
        chart_df = agg
    else:
        # Single plant: exact sheet if asked, else the plant with the most generation.
        if req.upper() != "ALL" and req[:31] in frames:
            chart_label = req[:31]
        else:
            chart_label = max(frames, key=lambda k: frames[k]["forecast_mw"].sum())
        chart_df = frames[chart_label].sort_values("timestamp")

    chart_df = chart_df.copy()
    chart_df["timestamp"] = chart_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M")
    response["series"] = chart_df[["timestamp", "forecast_mw"]].round(3).to_dict(orient="records")
    response["chart_sheet"] = chart_label
    response["n_plants"] = len(frames)
    response["total_points"] = len(chart_df)
    return response

@app.post("/api/run-forecast")
def run_forecast(req: ForecastRequest):
    if not FORECASTER_FILE.exists():
        return JSONResponse({"error": "combined_forecaster.py not found in backend folder."}, status_code=500)

    # --- ADD THESE 3 LINES: Automatically fetch weather for the selected date! ---
    weather_script = BASE_DIR / "fetch_openmeteo_weather.py"
    if weather_script.exists():
        subprocess.run(["python", str(weather_script), req.date], cwd=str(BASE_DIR))
    # -----------------------------------------------------------------------------

    output_id = uuid.uuid4().hex[:12]
    output_path = OUTPUT_DIR / f"forecast_{req.date}_{output_id}.xlsx"
    
    # ... (Keep the rest of your run_forecast code exactly the same) ...

    cmd = [
        "python", str(FORECASTER_FILE),
        "--date", req.date,
        "--horizon", str(req.horizon),
        "--models-dir", req.models_dir,
        "--output", str(output_path),
    ]

    if req.plant and req.plant.upper() != "ALL":
        cmd.extend(["--plant", req.plant.strip()])
    if req.family:
        cmd.extend(["--family", req.family])
    if req.evaluate:
        cmd.append("--evaluate")
    if req.ensemble:
        cmd.append("--ensemble")

    try:
        result = subprocess.run(cmd, cwd=str(BASE_DIR), text=True, capture_output=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Forecast process timed out."}, status_code=504)

    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "Forecast failed").strip()
        return JSONResponse({"error": error_text[-4000:]}, status_code=500)

    parsed = _parse_excel_output(output_path, req.plant)
    parsed["download_url"] = f"/api/download-forecast/{output_path.name}"
    parsed["log"] = (result.stdout + "\n" + result.stderr)[-4000:]
    return parsed

@app.get("/api/download-forecast/{filename}")
def download_forecast(filename: str):
    safe_name = Path(filename).name
    path = OUTPUT_DIR / safe_name
    if not path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=safe_name)


# ── Daily Data Delivery (DSS column-count report over SFTP) ───────────────────
import dss_delivery
from datetime import datetime as _dt


@app.get("/api/daily-delivery")
def get_daily_delivery(date: str = Query(None, description="Run day YYYY-MM-DD; defaults to today")):
    """
    DSS availability report for a run day.
    DA2 file = run day + 1, R16 file = run day - 1 (matches parse_mail.py).
    """
    try:
        base_day = _dt.strptime(date, "%Y-%m-%d").date() if date else date.today()
    except ValueError:
        return JSONResponse({"error": "Invalid date. Use YYYY-MM-DD."}, status_code=400)
    return dss_delivery.build_report(base_day, apply_offset=False)


@app.get("/api/daily-delivery/download")
def download_daily_delivery(file: str = Query(..., description="DSS CSV filename, e.g. 20260605_00_25.csv")):
    """Streams one DSS CSV (fetched live over SFTP). Filename is whitelist-validated."""
    if not dss_delivery.is_valid_dss_filename(file):
        return JSONResponse({"error": "Invalid filename."}, status_code=400)
    try:
        content = dss_delivery.fetch_one(file)
    except Exception as e:
        return JSONResponse({"error": f"Fetch failed: {e}"}, status_code=502)
    if content is None:
        return JSONResponse({"error": "File not found on server."}, status_code=404)
    safe = Path(file).name
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )
# ════════════════════════════════════════════════════════════════════════════
# PASTE THIS INTO main.py
# Put it right after the  @app.get("/api/daily-delivery/download")  function,
# and BEFORE the  "# --- ADD THIS TO THE VERY END OF main.py ---"  static mount.
# Requires (already present in your file): get_connection, Query, JSONResponse,
# and  from datetime import datetime as _dt  (added with the daily-delivery block).
# ════════════════════════════════════════════════════════════════════════════

# ── Daily Data Delivery: per-company DB load status ───────────────────────────
_DB_EXPECTED_DSS = 212

# Raw revision code in each company table -> logical label shown in the UI.
_DB_REV_MAP = {
    "ISPL":    {"R20": "R16", "R02": "DA2"},
    "QUENEXT": {"R-16": "R16", "Rd2": "DA2"},
}
# Candidate table names (the ISPL table spelling varies in some schemas).
_DB_TABLES = {
    "ISPL":    ["dss_forecast_ipsl", "dss_forecast_ispl"],
    "QUENEXT": ["dss_forecast_quenext"],
}


def _resolve_table(cursor, candidates):
    for t in candidates:
        try:
            cursor.execute(f"SELECT 1 FROM `{t}` LIMIT 1")
            cursor.fetchall()
            return t
        except Exception:
            continue
    return None


def _db_status_for(company, target_date):
    rev_map = _DB_REV_MAP[company]
    revisions = {
        "R16": {"dss": 0, "blocks": 0, "present": False, "raw": None},
        "DA2": {"dss": 0, "blocks": 0, "present": False, "raw": None},
    }
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        table = _resolve_table(cursor, _DB_TABLES[company])
        if not table:
            return {"error": f"No DB table found for {company} "
                             f"(tried: {', '.join(_DB_TABLES[company])}).",
                    "revisions": revisions}
        sql = f"""
            SELECT revision_number,
                   COUNT(DISTINCT dss_id)   AS dss_count,
                   COUNT(DISTINCT block_no) AS blocks
            FROM `{table}`
            WHERE forecast_date = %s
            GROUP BY revision_number
        """
        cursor.execute(sql, (target_date,))
        rows = cursor.fetchall()
    except Exception as e:
        return {"error": f"DB query failed for {company}: {e}", "revisions": revisions}
    finally:
        cursor.close()
        conn.close()

    for r in rows:
        raw = str(r["revision_number"]).strip()
        logical = rev_map.get(raw)
        if logical is None:
            norm = raw.upper().replace("-", "").replace(" ", "")
            for k, v in rev_map.items():
                if k.upper().replace("-", "").replace(" ", "") == norm:
                    logical = v
                    break
        if logical in revisions:
            cnt = int(r["dss_count"])
            revisions[logical] = {
                "dss": cnt,
                "blocks": int(r["blocks"]),
                "present": cnt > 0,
                "raw": raw,
            }
    return {"error": None, "revisions": revisions}


@app.get("/api/daily-delivery/db")
def daily_delivery_db(date: str = Query(None, description="forecast_date YYYY-MM-DD; default today")):
    """DB load status (DSS IDs / 212) for ISPL and Quenext, by revision (R16 & DA2)."""
    try:
        target = _dt.strptime(date, "%Y-%m-%d").date() if date else _dt.now().date()
    except ValueError:
        return JSONResponse({"error": "Invalid date. Use YYYY-MM-DD."}, status_code=400)
    return {
        "date": target.isoformat(),
        "expected": _DB_EXPECTED_DSS,
        "companies": {
            "ISPL":    _db_status_for("ISPL", target),
            "Quenext": _db_status_for("QUENEXT", target),
        },
    }
# ── Daily Data Delivery: download R16 / DA2 forecast from DB as CSV ───────────
# PASTE this block into main.py right AFTER the  daily_delivery_db()  function
# (the  @app.get("/api/daily-delivery/db")  one), and BEFORE the daytime block.
# Reuses _DB_TABLES, _resolve_table, get_connection, pandas (pd), Query, Response.

# logical revision -> raw code stored in each company table
_DB_REV_RAW = {
    "ISPL":    {"R16": "R20",  "DA2": "R02"},
    "QUENEXT": {"R16": "R-16", "DA2": "Rd2"},
}


def _fmt_csv_time(t):
    if t is None:
        return ""
    if isinstance(t, str):
        return t[:5]
    try:
        secs = int(t.total_seconds())
        return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}"
    except Exception:
        return str(t)


@app.get("/api/daily-delivery/db-download")
def daily_delivery_db_download(
    company: str = Query(..., description="ISPL or Quenext"),
    revision: str = Query(..., description="R16 or DA2"),
    date: str = Query(..., description="forecast_date YYYY-MM-DD"),
):
    """
    Builds a CSV (block_time + one column per DSS, values = forecast_mw) for the
    given company/revision/date, straight from the DB. Works anywhere the DB is
    reachable (incl. Replit), unlike the SFTP file download.
    """
    comp = "QUENEXT" if "QUE" in company.upper() else "ISPL"
    rev = revision.upper().replace("-", "").replace(" ", "")
    rev = "DA2" if rev in ("DA2", "RD2", "R02") else "R16"

    raw_rev = _DB_REV_RAW[comp][rev]
    raw_norm = raw_rev.upper().replace("-", "").replace(" ", "")

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        table = _resolve_table(cursor, _DB_TABLES[comp])
        if not table:
            return JSONResponse({"error": f"No DB table for {comp}."}, status_code=404)
        sql = f"""
            SELECT dss_id, block_no, block_time, timestamp, forecast_mw
            FROM `{table}`
            WHERE forecast_date = %s
              AND REPLACE(REPLACE(UPPER(revision_number), '-', ''), ' ', '') = %s
            ORDER BY block_no, dss_id
        """
        cursor.execute(sql, (date, raw_norm))
        rows = cursor.fetchall()
    except Exception as e:
        return JSONResponse({"error": f"DB query failed: {e}"}, status_code=500)
    finally:
        cursor.close()
        conn.close()

    if not rows:
        return JSONResponse(
            {"error": f"No {company} {revision} rows for {date}."}, status_code=404)

    df = pd.DataFrame(rows)
    df["block_time"] = df["block_time"].apply(_fmt_csv_time)
    df["timestamp"] = df["timestamp"].astype(str)

    pivot = df.pivot_table(
        index=["block_no", "block_time", "timestamp"],
        columns="dss_id", values="forecast_mw", aggfunc="first",
    ).reset_index().sort_values("block_no")

    csv_text = pivot.to_csv(index=False)
    fname = f"{comp}_{rev}_{date}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
# ── Daytime 0/NULL check (solar can't be 0/null between 05:30 and 19:00) ──────
# PASTE this block into main.py ABOVE the StaticFiles mount line:
#     app.mount("/", StaticFiles(directory=ui_path, html=True), name="ui")
#
# Relies on already-present imports: get_connection, Query, JSONResponse.

from datetime import datetime as _dtime, timedelta as _td

# (company_label, logical_revision, table, normalized_revision_code)
_DAYTIME_GROUPS = [
    ("ISPL",    "R16", "dss_forecast_ipsl",    "R20"),  # ISPL intra-day
    ("ISPL",    "DA2", "dss_forecast_ipsl",    "R02"),  # ISPL day-ahead
    ("Quenext", "R16", "dss_forecast_quenext", "R16"),  # Quenext intra-day (stored 'R-16')
    ("Quenext", "DA2", "dss_forecast_quenext", "RD2"),  # Quenext day-ahead
]
_DAY_START = "05:30:00"
_DAY_END   = "19:00:00"


def _daytime_slot_labels():
    """15-min slot labels 05:30..19:00 inclusive as 'HH:MM'."""
    out, cur, end = [], _dtime(2000, 1, 1, 5, 30), _dtime(2000, 1, 1, 19, 0)
    while cur <= end:
        out.append(cur.strftime("%H:%M"))
        cur += _td(minutes=15)
    return out


def _fmt_block_time(t):
    """block_time may be timedelta (MySQL TIME) or str -> 'HH:MM'."""
    if t is None:
        return None
    if isinstance(t, str):
        return t[:5]
    try:
        secs = int(t.total_seconds())
    except AttributeError:
        return str(t)[:5]
    return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}"


def _run_daytime_group(cursor, company, revision, table, rev_code, slots, date):
    sql = f"""
        SELECT dss_id, block_time, forecast_mw
        FROM {table}
        WHERE forecast_date = %s
          AND REPLACE(REPLACE(UPPER(revision_number), '-', ''), ' ', '') = %s
          AND block_time BETWEEN %s AND %s
        ORDER BY dss_id, block_time
    """
    group = {"company": company, "revision": revision,
             "flagged": [], "flagged_count": 0, "total_dss": 0, "error": None}
    try:
        cursor.execute(sql, (date, rev_code, _DAY_START, _DAY_END))
        rows = cursor.fetchall()
    except Exception as e:
        group["error"] = str(e)
        return group

    per_dss = {}
    for r in rows:
        slot = _fmt_block_time(r["block_time"])
        if slot is None:
            continue
        per_dss.setdefault(r["dss_id"], {})[slot] = r["forecast_mw"]

    group["total_dss"] = len(per_dss)
    for dss in sorted(per_dss.keys()):
        cells = per_dss[dss]
        bad, out_cells = [], {}
        for s in slots:
            v = cells.get(s, None)                       # missing slot = bad too
            out_cells[s] = None if v is None else round(float(v), 3)
            if (v is None) or (float(v) == 0.0):
                bad.append(s)
        if bad:
            group["flagged"].append({"dss_id": dss, "cells": out_cells, "bad": bad})
    group["flagged_count"] = len(group["flagged"])
    return group


@app.get("/api/daily-delivery/daytime-check")
def daily_delivery_daytime_check(date: str = Query(..., description="Forecast date YYYY-MM-DD")):
    """
    Flags DSS IDs (ISPL & Quenext, revisions R16 & DA2) with a 0 or NULL
    forecast_mw in any 15-min slot between 05:30 and 19:00 on the given date.
    """
    try:
        _dtime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse({"error": "Invalid date. Use YYYY-MM-DD."}, status_code=400)

    slots = _daytime_slot_labels()
    result = {"date": date, "window": {"start": "05:30", "end": "19:00"},
              "slots": slots, "groups": {}}

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        for company, revision, table, rev_code in _DAYTIME_GROUPS:
            result["groups"][f"{company}|{revision}"] = _run_daytime_group(
                cursor, company, revision, table, rev_code, slots, date)
    finally:
        cursor.close()
        conn.close()

    return result
# --- ADD THIS TO THE VERY END OF main.py ---
# This tells FastAPI that your HTML files are in the 'ui' folder
ui_path = os.path.join(os.path.dirname(__file__), "ui")
if os.path.exists(ui_path):
    app.mount("/", StaticFiles(directory=ui_path, html=True), name="ui")
else:
    print(f"WARNING: UI directory not found at {ui_path}. Static files will not be served.")