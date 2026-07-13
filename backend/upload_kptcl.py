"""
upload_kptcl.py — Parse the KPTCL Excel file and load into power_market_db.

Handles 3 target areas (phase 1):
  1. `unitmaster`  sheet  → unit_master table  (simple rows)
  2. `cost`        sheet  → generation_cost_master table (simple rows)
  3. 6 wide sheets (URS, SCH, ENT, Backdown, Entitlement, Calculated-DC)
     → merged into plant_block_data (one row per timestamp_id + unit)

ID resolution:
  - unit_name → unit_id   via unit_master
  - (date, block_no) → timestamp_id   via plant_block_key  (created if missing)

Skip-if-exists: rows already present are not re-inserted.
schedule column is STORED GENERATED — never written.
"""
import hashlib
from datetime import datetime, date, time, timedelta
from io import BytesIO
from collections import defaultdict

import openpyxl
import pymysql


# ======================= HELPERS =======================

def _num(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _int(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None

def _str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None

def _date(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def _bool(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return 1
    if s in ("false", "0", "no"):
        return 0
    return None

def _row_hash(fields: dict) -> str:
    parts = [f"{k}={'' if v is None else v}" for k, v in sorted(fields.items())]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

def _find_header_row(ws, marker="date"):
    """Find the row index (1-based) where col A (stripped, lowered) == marker."""
    for i, row in enumerate(ws.iter_rows(max_col=2, values_only=True), 1):
        if row[0] is not None and str(row[0]).strip().lower() == marker:
            return i
    return None

def _block_time(block_no):
    """block_no 1 → 00:00, 2 → 00:15, ... 96 → 23:45"""
    mins = (block_no - 1) * 15
    return time(mins // 60, mins % 60)


# ======================= UNIT MASTER =======================

def _parse_unitmaster(wb):
    ws = wb["unitmaster"]
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        un = _str(r[0])
        if not un:
            continue
        rows.append({
            "unit_name": un,
            "generator": _str(r[1]),
            "plant_type": _str(r[2]),
            "fuel_type": _str(r[3]),
            "total_capacity": _num(r[4]),
            "contracted_capacity": _num(r[5]),
            "min_technical_limit": _num(r[6]),
            "time_between_ramp_up_down": _num(r[7]),
            "ramp_rate_mw_per_min": _num(r[8]),
            "pool": _str(r[9]),
            "must_run": _bool(r[10]),
            "variable_cost": _num(r[11]),
        })
    return rows

def _load_unitmaster(conn, rows, source_file):
    cur = conn.cursor()
    inserted = 0
    for r in rows:
        rh = _row_hash(r)
        try:
            cur.execute(
                """INSERT INTO unit_master
                     (unit_name, generator, plant_type, fuel_type,
                      total_capacity, contracted_capacity, min_technical_limit,
                      time_between_ramp_up_down, ramp_rate_mw_per_min,
                      pool, must_run, variable_cost, row_hash, source_file)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE id=id""",
                (r["unit_name"], r["generator"], r["plant_type"], r["fuel_type"],
                 r["total_capacity"], r["contracted_capacity"], r["min_technical_limit"],
                 r["time_between_ramp_up_down"], r["ramp_rate_mw_per_min"],
                 r["pool"], r["must_run"], r["variable_cost"], rh, source_file)
            )
            if cur.rowcount == 1:
                inserted += 1
        except pymysql.err.IntegrityError:
            pass  # skip duplicate
    conn.commit()
    cur.close()
    return inserted


# ======================= GENERATION COST MASTER =======================

def _parse_cost(wb):
    ws = wb["cost"]
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        un = _str(r[0])
        if not un:
            continue
        rows.append({
            "unit_name": un,
            "generator": _str(r[1]),
            "station_name": _str(r[2]),
            "acronym": _str(r[3]),
            "contracted_capacity": _num(r[4]),
            "valid_from": _date(r[5]),
            "valid_to": _date(r[6]),
            "fixed_cost": _num(r[7]),
            "variable_cost": _num(r[8]),
            "must_run": _bool(r[9]),
            "mini_limit": _num(r[10]),
            "pool": _str(r[11]),
            "plant_type": _str(r[12]),
            "entire_shared_ent": _num(r[13]),
            "entitlement_per_unit": _num(r[14]),
            "no_of_units": _int(r[15]),
            "mintech_per_unit": _num(r[16]),
            "mini_limit_percent": _num(r[17]),
        })
    return rows

def _load_cost(conn, rows, source_file):
    cur = conn.cursor()
    inserted = 0
    for r in rows:
        rh = _row_hash(r)
        try:
            cur.execute(
                """INSERT INTO generation_cost_master
                     (unit_name, generator, station_name, acronym,
                      contracted_capacity, valid_from, valid_to,
                      fixed_cost, variable_cost, must_run, mini_limit,
                      pool, plant_type, entire_shared_ent,
                      entitlement_per_unit, no_of_units,
                      mintech_per_unit, mini_limit_percent,
                      row_hash, source_file)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE id=id""",
                (r["unit_name"], r["generator"], r["station_name"], r["acronym"],
                 r["contracted_capacity"], r["valid_from"], r["valid_to"],
                 r["fixed_cost"], r["variable_cost"], r["must_run"], r["mini_limit"],
                 r["pool"], r["plant_type"], r["entire_shared_ent"],
                 r["entitlement_per_unit"], r["no_of_units"],
                 r["mintech_per_unit"], r["mini_limit_percent"],
                 rh, source_file)
            )
            if cur.rowcount == 1:
                inserted += 1
        except pymysql.err.IntegrityError:
            pass
    conn.commit()
    cur.close()
    return inserted


# ======================= PLANT BLOCK DATA (6 wide sheets) =======================

# Sheet name -> column in plant_block_data
_WIDE_SHEETS = {
    "URS":           "urs",
    "SCH":           "sch",
    "ENT":           "ent",
    "Backdown":      "backdown",
    "Entitlement":   "entitlement",
    "Calculated-DC": "calculated_dc",
}

def _parse_wide_sheets(wb):
    """Parse all 6 wide sheets into a merged dict:
       data[(date, block_no, unit_name)] = {urs:v, sch:v, ent:v, ...}
    """
    data = defaultdict(dict)
    for sheet_name, field in _WIDE_SHEETS.items():
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        hrow = _find_header_row(ws, "date")
        if hrow is None:
            continue
        # read header row to get unit names (col C onwards)
        header = list(ws.iter_rows(min_row=hrow, max_row=hrow, values_only=True))[0]
        units = []
        for i, h in enumerate(header):
            if i < 2:
                continue
            un = _str(h)
            if un:
                units.append((i, un))
        # read data rows
        for row in ws.iter_rows(min_row=hrow + 1, values_only=True):
            d = _date(row[0])
            b = _int(row[1])
            if d is None or b is None:
                continue
            for col_idx, un in units:
                if col_idx >= len(row):
                    continue
                val = _num(row[col_idx])
                data[(d, b, un)][field] = val
    return data


def _ensure_block_keys(conn, dates_blocks):
    """Make sure plant_block_key has entries for all (date, block_no) combos.
    Returns a dict: (date, block_no) -> block_id (= timestamp_id).
    """
    cur = conn.cursor()
    # bulk-read existing
    cur.execute("SELECT block_id, report_date, block_no FROM plant_block_key")
    existing = {}
    for r in cur.fetchall():
        existing[(r[1], r[2])] = r[0]

    # find max block_id for new entries
    cur.execute("SELECT COALESCE(MAX(block_id), 999) FROM plant_block_key")
    next_id = cur.fetchone()[0] + 1

    to_insert = []
    for (d, b) in sorted(dates_blocks):
        if (d, b) not in existing:
            bt = _block_time(b)
            to_insert.append((next_id, d, b, bt))
            existing[(d, b)] = next_id
            next_id += 1

    if to_insert:
        cur.executemany(
            "INSERT IGNORE INTO plant_block_key (block_id, report_date, block_no, block_time) VALUES (%s,%s,%s,%s)",
            to_insert,
        )
        conn.commit()
    cur.close()
    return existing


def _resolve_unit_ids(conn):
    """Return dict: unit_name (upper) -> unit_id from unit_master."""
    cur = conn.cursor()
    cur.execute("SELECT unit_name, unit_id FROM unit_master WHERE unit_id IS NOT NULL")
    mapping = {str(r[0]).strip().upper(): r[1] for r in cur.fetchall()}
    cur.close()
    return mapping


def _load_plant_block_data(conn, data, source_file):
    """Insert merged wide-sheet data into plant_block_data (skip existing)."""
    cur = conn.cursor()

    # 1. resolve IDs
    dates_blocks = set((d, b) for (d, b, _) in data.keys())
    bk_map = _ensure_block_keys(conn, dates_blocks)
    uid_map = _resolve_unit_ids(conn)

    # 2. load existing (timestamp_id, unit_name) to skip dupes
    cur.execute("SELECT timestamp_id, unit_name FROM plant_block_data")
    existing = set((r[0], r[1]) for r in cur.fetchall())

    # 3. build rows
    to_insert = []
    skipped = 0
    unresolved_units = set()
    for (d, b, un), vals in data.items():
        ts_id = bk_map.get((d, b))
        if ts_id is None:
            continue
        u_id = uid_map.get(un.upper())
        if u_id is None:
            unresolved_units.add(un)
            u_id = None  # still insert with NULL unit_id, unit_name is present

        if (ts_id, un) in existing:
            skipped += 1
            continue

        to_insert.append((
            ts_id, u_id, un,
            vals.get("urs"), vals.get("sch"), vals.get("ent"),
            vals.get("backdown"), vals.get("entitlement"),
            vals.get("calculated_dc"), source_file,
        ))

    # 4. batch insert
    inserted = 0
    if to_insert:
        cur.executemany(
            """INSERT INTO plant_block_data
                 (timestamp_id, unit_id, unit_name,
                  urs, sch, ent, backdown, entitlement,
                  calculated_dc, source_file)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            to_insert,
        )
        inserted = cur.rowcount
        conn.commit()
    cur.close()
    return inserted, skipped, unresolved_units


# ======================= MAIN ENTRY POINT =======================

def process_upload(file_bytes: bytes, filename: str, db_config: dict):
    """Process an uploaded Excel file. Returns a summary dict."""
    wb = openpyxl.load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
    conn = pymysql.connect(**db_config, charset="utf8mb4", autocommit=False)
    conn.cursor().execute("USE power_market_db")

    summary = {"filename": filename, "tables": {}}
    errors = []

    try:
        # 1. Unit Master
        if "unitmaster" in wb.sheetnames:
            try:
                rows = _parse_unitmaster(wb)
                ins = _load_unitmaster(conn, rows, filename)
                summary["tables"]["unit_master"] = {"parsed": len(rows), "inserted": ins, "skipped": len(rows) - ins}
            except Exception as e:
                errors.append(f"unitmaster: {e}")

        # 2. Cost
        if "cost" in wb.sheetnames:
            try:
                rows = _parse_cost(wb)
                ins = _load_cost(conn, rows, filename)
                summary["tables"]["generation_cost_master"] = {"parsed": len(rows), "inserted": ins, "skipped": len(rows) - ins}
            except Exception as e:
                errors.append(f"cost: {e}")

        # 3. Plant Block Data (6 wide sheets)
        try:
            data = _parse_wide_sheets(wb)
            if data:
                ins, skip, unresolved = _load_plant_block_data(conn, data, filename)
                summary["tables"]["plant_block_data"] = {
                    "parsed": len(data),
                    "inserted": ins,
                    "skipped": skip,
                    "unresolved_units": sorted(unresolved) if unresolved else [],
                }
            else:
                summary["tables"]["plant_block_data"] = {"parsed": 0, "inserted": 0, "skipped": 0}
        except Exception as e:
            errors.append(f"plant_block_data: {e}")

    finally:
        conn.close()
        wb.close()

    summary["errors"] = errors
    summary["status"] = "success" if not errors else "partial"
    return summary