# settlement_api.py — PSS Billing / Settlement router (extracted from your settlement app)
# Included by main.py via: app.include_router(settlement_router)
# Routes are namespaced under /api/settlement/*

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, PlainTextResponse
import pymysql
from decimal import Decimal
from datetime import date, datetime
import csv
import io


router = APIRouter(prefix="/api/settlement", tags=["settlement"])


DB = {
    "host": "65.1.28.178",
    "port": 3306,
    "user": "energy",
    "password": "Energy@123",
    "database": "energy_monitor",
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}


DEFAULT_REPORT_DATE = "2026-06-15"
DEFAULT_BILLING_MONTH = "2026-06-01"


def get_conn():
    return pymysql.connect(**DB)


def json_safe(value):
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def prefix(alias):
    return f"{alias}." if alias else ""


def build_payment_where(
    billing_month,
    report_date,
    company="All",
    mapping_type="All",
    billing_group="All",
    search="",
    alias="",
):
    p = prefix(alias)

    where = [
        f"{p}billing_month = %s",
        f"{p}report_date = %s",
    ]
    params = [billing_month, report_date]

    if company != "All":
        where.append(f"{p}company_name = %s")
        params.append(company)

    if mapping_type != "All":
        where.append(f"{p}mapping_type = %s")
        params.append(mapping_type)

    if billing_group != "All":
        where.append(f"{p}billing_group = %s")
        params.append(billing_group)

    if search and search.strip():
        s = f"%{search.strip()}%"
        where.append(
            f"""
            (
                CAST({p}billing_unit_no AS CHAR) LIKE %s
                OR COALESCE({p}source_sr_nos, '') LIKE %s
                OR COALESCE({p}pss_name, '') LIKE %s
                OR COALESCE({p}dss_ids, '') LIKE %s
                OR COALESCE({p}uss_ids, '') LIKE %s
                OR COALESCE({p}poi_ids, '') LIKE %s
            )
            """
        )
        params.extend([s, s, s, s, s, s])

    return " AND ".join(where), params


def build_capacity_where(
    billing_month,
    report_date,
    mapping_type="All",
    billing_group="All",
    search="",
    alias="",
):
    p = prefix(alias)

    where = [
        f"{p}billing_month = %s",
        f"{p}report_date = %s",
    ]
    params = [billing_month, report_date]

    if mapping_type != "All":
        where.append(f"{p}mapping_type = %s")
        params.append(mapping_type)

    if billing_group != "All":
        where.append(f"{p}billing_group = %s")
        params.append(billing_group)

    if search and search.strip():
        s = f"%{search.strip()}%"
        where.append(
            f"""
            (
                CAST({p}billing_unit_no AS CHAR) LIKE %s
                OR COALESCE({p}source_sr_nos, '') LIKE %s
                OR COALESCE({p}pss_name, '') LIKE %s
                OR COALESCE({p}dss_ids, '') LIKE %s
                OR COALESCE({p}uss_ids, '') LIKE %s
                OR COALESCE({p}poi_ids, '') LIKE %s
            )
            """
        )
        params.extend([s, s, s, s, s, s])

    return " AND ".join(where), params


@router.get("/overview")
def overview(
    billing_month: str = Query(DEFAULT_BILLING_MONTH),
    report_date: str = Query(DEFAULT_REPORT_DATE),
    company: str = Query("All"),
    mapping_type: str = Query("All"),
    billing_group: str = Query("All"),
    search: str = Query(""),
):
    conn = get_conn()

    try:
        payment_where_sql, payment_params = build_payment_where(
            billing_month=billing_month,
            report_date=report_date,
            company=company,
            mapping_type=mapping_type,
            billing_group=billing_group,
            search=search,
        )

        payment_where_alias_sql, payment_alias_params = build_payment_where(
            billing_month=billing_month,
            report_date=report_date,
            company=company,
            mapping_type=mapping_type,
            billing_group=billing_group,
            search=search,
            alias="p",
        )

        capacity_where_alias_sql, capacity_alias_params = build_capacity_where(
            billing_month=billing_month,
            report_date=report_date,
            mapping_type=mapping_type,
            billing_group=billing_group,
            search=search,
            alias="b",
        )

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS billing_rows,
                    COUNT(DISTINCT company_name) AS companies,
                    COUNT(DISTINCT billing_unit_no) AS billing_units,
                    SUM(fixed_amount) AS fixed_amount,
                    SUM(da_amount) AS da_amount,
                    SUM(id_amount) AS id_amount,
                    SUM(total_payable) AS total_payable
                FROM pss_billing_by_unit
                WHERE {payment_where_sql}
                """,
                payment_params,
            )
            kpi_payment = cur.fetchone() or {}

            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS billing_units,
                    SUM(solar_capacity_mw) AS solar_mw,
                    SUM(wind_capacity_mw) AS wind_mw,
                    SUM(total_capacity_mw) AS total_mw,
                    SUM(base_monthly_amount) AS base_monthly_amount,
                    SUM(plant_count) AS plant_count
                FROM (
                    SELECT
                        b.billing_unit_no,
                        MAX(b.solar_capacity_mw) AS solar_capacity_mw,
                        MAX(b.wind_capacity_mw) AS wind_capacity_mw,
                        MAX(b.total_capacity_mw) AS total_capacity_mw,
                        MAX(b.base_monthly_amount) AS base_monthly_amount,
                        COALESCE(MAX(pc.plant_count), 1) AS plant_count
                    FROM pss_billing_by_unit b
                    LEFT JOIN (
                        SELECT
                            report_date,
                            billing_unit_no,
                            COUNT(*) AS plant_count
                        FROM pss_dss_uss_mapping
                        WHERE report_date = %s
                        GROUP BY report_date, billing_unit_no
                    ) pc
                    ON b.report_date = pc.report_date
                    AND b.billing_unit_no = pc.billing_unit_no
                    WHERE {capacity_where_alias_sql}
                    GROUP BY b.billing_unit_no
                ) x
                """,
                [report_date] + capacity_alias_params,
            )
            kpi_capacity = cur.fetchone() or {}

            kpi = {
                "billing_rows": kpi_payment.get("billing_rows") or 0,
                "companies": kpi_payment.get("companies") or 0,
                "billing_units": kpi_capacity.get("billing_units") or 0,
                "plant_count": kpi_capacity.get("plant_count") or 0,
                "solar_mw": kpi_capacity.get("solar_mw") or 0,
                "wind_mw": kpi_capacity.get("wind_mw") or 0,
                "total_mw": kpi_capacity.get("total_mw") or 0,
                "base_monthly_amount": kpi_capacity.get("base_monthly_amount") or 0,
                "fixed_amount": kpi_payment.get("fixed_amount") or 0,
                "da_amount": kpi_payment.get("da_amount") or 0,
                "id_amount": kpi_payment.get("id_amount") or 0,
                "total_payable": kpi_payment.get("total_payable") or 0,
            }

            cur.execute(
                f"""
                SELECT
                    company_name,
                    COUNT(*) AS billing_rows,
                    COUNT(DISTINCT billing_unit_no) AS billing_units,
                    SUM(base_monthly_amount) AS base_monthly_amount,
                    SUM(fixed_amount) AS fixed_amount,
                    SUM(da_amount) AS da_amount,
                    SUM(id_amount) AS id_amount,
                    SUM(total_payable) AS total_payable
                FROM pss_billing_by_unit
                WHERE {payment_where_sql}
                GROUP BY company_name
                ORDER BY company_name
                """,
                payment_params,
            )
            company_summary = cur.fetchall()

            cur.execute(
                f"""
                SELECT
                    p.mapping_type,
                    p.billing_rows,
                    p.billing_units,
                    COALESCE(c.plant_count, 0) AS plant_count,
                    COALESCE(c.solar_mw, 0) AS solar_mw,
                    COALESCE(c.wind_mw, 0) AS wind_mw,
                    COALESCE(c.total_mw, 0) AS total_mw,
                    COALESCE(c.base_monthly_amount, 0) AS base_monthly_amount,
                    p.total_payable
                FROM (
                    SELECT
                        mapping_type,
                        COUNT(*) AS billing_rows,
                        COUNT(DISTINCT billing_unit_no) AS billing_units,
                        SUM(total_payable) AS total_payable
                    FROM pss_billing_by_unit
                    WHERE {payment_where_sql}
                    GROUP BY mapping_type
                ) p
                LEFT JOIN (
                    SELECT
                        mapping_type,
                        SUM(plant_count) AS plant_count,
                        SUM(solar_capacity_mw) AS solar_mw,
                        SUM(wind_capacity_mw) AS wind_mw,
                        SUM(total_capacity_mw) AS total_mw,
                        SUM(base_monthly_amount) AS base_monthly_amount
                    FROM (
                        SELECT
                            b.billing_unit_no,
                            MAX(b.mapping_type) AS mapping_type,
                            MAX(b.solar_capacity_mw) AS solar_capacity_mw,
                            MAX(b.wind_capacity_mw) AS wind_capacity_mw,
                            MAX(b.total_capacity_mw) AS total_capacity_mw,
                            MAX(b.base_monthly_amount) AS base_monthly_amount,
                            COALESCE(MAX(pc.plant_count), 1) AS plant_count
                        FROM pss_billing_by_unit b
                        LEFT JOIN (
                            SELECT
                                report_date,
                                billing_unit_no,
                                COUNT(*) AS plant_count
                            FROM pss_dss_uss_mapping
                            WHERE report_date = %s
                            GROUP BY report_date, billing_unit_no
                        ) pc
                        ON b.report_date = pc.report_date
                        AND b.billing_unit_no = pc.billing_unit_no
                        WHERE {capacity_where_alias_sql}
                        GROUP BY b.billing_unit_no
                    ) x
                    GROUP BY mapping_type
                ) c
                ON p.mapping_type = c.mapping_type
                ORDER BY p.mapping_type
                """,
                payment_params + [report_date] + capacity_alias_params,
            )
            type_summary = cur.fetchall()

            cur.execute(
                f"""
                SELECT
                    billing_group,
                    COUNT(*) AS billing_units,
                    SUM(plant_count) AS plant_count,

                    SUM(solar_capacity_mw) AS solar_capacity_mw,
                    SUM(wind_capacity_mw) AS wind_capacity_mw,
                    SUM(total_capacity_mw) AS total_capacity_mw,

                    SUM(solar_monthly_amount) AS solar_monthly_amount,
                    SUM(wind_monthly_amount) AS wind_monthly_amount,
                    SUM(total_monthly_amount) AS total_monthly_amount,

                    SUM(solar_yearly_amount) AS solar_yearly_amount,
                    SUM(wind_yearly_amount) AS wind_yearly_amount,
                    SUM(total_yearly_amount) AS total_yearly_amount
                FROM (
                    SELECT
                        b.billing_unit_no,
                        MAX(b.billing_group) AS billing_group,
                        COALESCE(MAX(pc.plant_count), 1) AS plant_count,

                        MAX(b.solar_capacity_mw) AS solar_capacity_mw,
                        MAX(b.wind_capacity_mw) AS wind_capacity_mw,
                        MAX(b.total_capacity_mw) AS total_capacity_mw,

                        MAX(b.solar_base_amount) AS solar_monthly_amount,
                        MAX(b.wind_base_amount) AS wind_monthly_amount,
                        MAX(b.base_monthly_amount) AS total_monthly_amount,

                        MAX(b.solar_base_amount) * 12 AS solar_yearly_amount,
                        MAX(b.wind_base_amount) * 12 AS wind_yearly_amount,
                        MAX(b.base_monthly_amount) * 12 AS total_yearly_amount
                    FROM pss_billing_by_unit b
                    LEFT JOIN (
                        SELECT
                            report_date,
                            billing_unit_no,
                            COUNT(*) AS plant_count
                        FROM pss_dss_uss_mapping
                        WHERE report_date = %s
                        GROUP BY report_date, billing_unit_no
                    ) pc
                    ON b.report_date = pc.report_date
                    AND b.billing_unit_no = pc.billing_unit_no
                    WHERE {capacity_where_alias_sql}
                    GROUP BY b.billing_unit_no
                ) x
                GROUP BY billing_group
                ORDER BY billing_group
                """,
                [report_date] + capacity_alias_params,
            )
            group_range_counts = cur.fetchall()

            cur.execute(
                f"""
                SELECT
                    p.company_name,
                    p.billing_group,
                    COUNT(*) AS billing_rows,
                    COUNT(DISTINCT p.billing_unit_no) AS billing_units,
                    COALESCE(SUM(pc.plant_count), COUNT(DISTINCT p.billing_unit_no)) AS plant_count,
                    SUM(p.base_monthly_amount) AS base_monthly_amount,
                    SUM(p.fixed_amount) AS fixed_amount,
                    SUM(p.da_amount) AS da_amount,
                    SUM(p.id_amount) AS id_amount,
                    SUM(p.total_payable) AS total_payable
                FROM pss_billing_by_unit p
                LEFT JOIN (
                    SELECT
                        report_date,
                        billing_unit_no,
                        COUNT(*) AS plant_count
                    FROM pss_dss_uss_mapping
                    WHERE report_date = %s
                    GROUP BY report_date, billing_unit_no
                ) pc
                ON p.report_date = pc.report_date
                AND p.billing_unit_no = pc.billing_unit_no
                WHERE {payment_where_alias_sql}
                GROUP BY p.company_name, p.billing_group
                ORDER BY p.billing_group, p.company_name
                """,
                [report_date] + payment_alias_params,
            )
            company_group_summary = cur.fetchall()

            cur.execute(
                f"""
                SELECT
                    company_name,
                    mapping_type,
                    COUNT(*) AS billing_rows,
                    COUNT(DISTINCT billing_unit_no) AS billing_units,
                    SUM(total_payable) AS total_payable
                FROM pss_billing_by_unit
                WHERE {payment_where_sql}
                GROUP BY company_name, mapping_type
                ORDER BY company_name, mapping_type
                """,
                payment_params,
            )
            company_type_summary = cur.fetchall()

            cur.execute(
                f"""
                SELECT
                    company_name,
                    da_revision AS revision,
                    'DA' AS component,
                    COUNT(*) AS billing_rows,
                    SUM(da_amount) AS amount
                FROM pss_billing_by_unit
                WHERE {payment_where_sql}
                GROUP BY company_name, da_revision

                UNION ALL

                SELECT
                    company_name,
                    id_revision AS revision,
                    'ID' AS component,
                    COUNT(*) AS billing_rows,
                    SUM(id_amount) AS amount
                FROM pss_billing_by_unit
                WHERE {payment_where_sql}
                GROUP BY company_name, id_revision

                ORDER BY company_name, component
                """,
                payment_params + payment_params,
            )
            revision_summary = cur.fetchall()

        return JSONResponse(
            json_safe(
                {
                    "status": "success",
                    "kpi": kpi,
                    "company_summary": company_summary,
                    "type_summary": type_summary,
                    "group_range_counts": group_range_counts,
                    "company_group_summary": company_group_summary,
                    "company_type_summary": company_type_summary,
                    "revision_summary": revision_summary,
                }
            )
        )

    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    finally:
        conn.close()


@router.get("/rows")
def rows(
    billing_month: str = Query(DEFAULT_BILLING_MONTH),
    report_date: str = Query(DEFAULT_REPORT_DATE),
    company: str = Query("All"),
    mapping_type: str = Query("All"),
    billing_group: str = Query("All"),
    search: str = Query(""),
    limit: int = Query(1000),
):
    conn = get_conn()

    try:
        where_sql, params = build_payment_where(
            billing_month=billing_month,
            report_date=report_date,
            company=company,
            mapping_type=mapping_type,
            billing_group=billing_group,
            search=search,
        )

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    billing_month,
                    report_date,
                    company_name,
                    billing_unit_no,
                    source_sr_nos,
                    pss_name,
                    mapping_type,
                    dss_ids,
                    uss_ids,
                    poi_ids,
                    solar_capacity_mw,
                    wind_capacity_mw,
                    total_capacity_mw,
                    billing_group,
                    base_yearly_amount,
                    base_monthly_amount,
                    solar_base_amount,
                    wind_base_amount,
                    fixed_amount,
                    da_amount,
                    id_amount,
                    total_payable,
                    da_revision,
                    id_revision,
                    solar_da_nrmse,
                    solar_id_nrmse,
                    wind_da_nrmse,
                    wind_id_nrmse,
                    solar_da_status,
                    solar_id_status,
                    wind_da_status,
                    wind_id_status
                FROM pss_billing_by_unit
                WHERE {where_sql}
                ORDER BY company_name, billing_unit_no
                LIMIT %s
                """,
                params + [limit],
            )
            data = cur.fetchall()

        return JSONResponse(json_safe({"status": "success", "rows": data}))

    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    finally:
        conn.close()


@router.get("/unit-detail")
def unit_detail(
    billing_month: str = Query(DEFAULT_BILLING_MONTH),
    report_date: str = Query(DEFAULT_REPORT_DATE),
    billing_unit_no: int = Query(...),
):
    conn = get_conn()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM pss_billing_by_unit
                WHERE billing_month = %s
                  AND report_date = %s
                  AND billing_unit_no = %s
                ORDER BY company_name
                """,
                (billing_month, report_date, billing_unit_no),
            )
            billing = cur.fetchall()

            cur.execute(
                """
                SELECT
                    report_date,
                    sr_no,
                    pss_name,
                    mapping_type,
                    billing_unit_no,
                    billing_unit_name,
                    merged_to_sr_no,
                    merged_to_pss_name,
                    dss_id,
                    uss_id,
                    poi_id,
                    solar_capacity_mw,
                    wind_capacity_mw,
                    total_capacity_mw
                FROM pss_dss_uss_mapping
                WHERE report_date = %s
                  AND billing_unit_no = %s
                ORDER BY sr_no, dss_id
                """,
                (report_date, billing_unit_no),
            )
            raw_rows = cur.fetchall()

        return JSONResponse(
            json_safe(
                {
                    "status": "success",
                    "billing": billing,
                    "raw_rows": raw_rows,
                }
            )
        )

    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    finally:
        conn.close()


@router.get("/export-csv")
def export_csv(
    billing_month: str = Query(DEFAULT_BILLING_MONTH),
    report_date: str = Query(DEFAULT_REPORT_DATE),
    company: str = Query("All"),
    mapping_type: str = Query("All"),
    billing_group: str = Query("All"),
):
    conn = get_conn()

    try:
        where_sql, params = build_payment_where(
            billing_month=billing_month,
            report_date=report_date,
            company=company,
            mapping_type=mapping_type,
            billing_group=billing_group,
            search="",
        )

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                FROM pss_billing_by_unit
                WHERE {where_sql}
                ORDER BY company_name, billing_unit_no
                """,
                params,
            )
            data = cur.fetchall()

        output = io.StringIO()

        if data:
            writer = csv.DictWriter(output, fieldnames=list(data[0].keys()))
            writer.writeheader()
            writer.writerows(data)
        else:
            output.write("No data found\n")

        filename = f"pss_billing_{billing_month}_{report_date}.csv"

        return PlainTextResponse(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except Exception as e:
        return PlainTextResponse(f"Error: {e}", status_code=500)

    finally:
        conn.close()