"""Seed data/veritas.duckdb with a synthetic Gilead-shaped analytics store:

raw layer   (S3-landed shape)   raw_s3_gross_sales, raw_s3_forecast
staging     (conformed)         stg_sales_conformed, stg_forecast_conformed
facts                           fct_revenue_actual, fct_revenue_forecast
dims                            dim_product, dim_geography, dim_forecast_cycle
mart                            mart_revenue_actual_vs_forecast

24 months of monthly actuals vs. two forecast cycles for six therapeutic
products across five geographies, with deliberate, documented imperfections
so UAT has something real to find (see IMPERFECTIONS below).

Run:  python data/seed_mart_db.py [--force]
(or set VERITAS_SEED_DEMO_MART=1 and the app seeds it at start-up when missing)
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app import config  # noqa: E402  (after sys.path setup)

DB = config.DB_PATH            # honours VERITAS_DB_PATH, like the app

BRANDS = [  # (brand, therapeutic_area, wac_unit_price)
    ("Biktarvy", "HIV", 3800.0),
    ("Descovy", "HIV", 2100.0),
    ("Genvoya", "HIV", 3100.0),
    ("Epclusa", "Liver Disease", 8200.0),
    ("Veklury", "Virology", 520.0),
    ("Trodelvy", "Oncology", 2450.0),
]
GEOS = [("US", "North America"), ("DE", "Europe"), ("FR", "Europe"),
        ("JP", "Intercontinental"), ("UK", "Europe")]
CYCLES = [("FC-2025-Q4", date(2025, 10, 15)), ("FC-2026-Q2", date(2026, 4, 15))]

# IMPERFECTIONS planted for UAT to catch (documented, deterministic):
#   1. raw_s3_gross_sales contains 3 duplicate shipment rows (dedup rule REQ).
#   2. One JP/Veklury month has NULL units in raw (completeness rule).
#   3. mart variance_amount is mis-computed (+0.02 rounding drift) for two
#      Trodelvy rows — the variance-formula test FAILs, demonstrating verdicts.
#   4. Forecast cycle FC-2026-Q2 lacks rows for month 2026-08 for Epclusa/UK
#      (late-arriving forecast).

MONTHS = [date(2024, 9, 1)]
while len(MONTHS) < 24:
    m = MONTHS[-1]
    MONTHS.append(date(m.year + (m.month == 12), (m.month % 12) + 1, 1))


def main(force: bool, db_path=None) -> None:
    global DB
    DB = Path(db_path or DB)
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB))
    if force:
        for t in ["mart_revenue_actual_vs_forecast", "fct_revenue_actual",
                  "fct_revenue_forecast", "stg_sales_conformed",
                  "stg_forecast_conformed", "raw_s3_gross_sales",
                  "raw_s3_forecast", "dim_product", "dim_geography",
                  "dim_forecast_cycle"]:
            con.execute(f"DROP TABLE IF EXISTS {t}")
    rng = random.Random(42)

    con.execute("""CREATE TABLE dim_product (
        product_id INTEGER PRIMARY KEY, brand VARCHAR, therapeutic_area VARCHAR,
        wac_unit_price DOUBLE)""")
    for i, (b, ta, p) in enumerate(BRANDS, 1):
        con.execute("INSERT INTO dim_product VALUES (?,?,?,?)", [i, b, ta, p])

    con.execute("""CREATE TABLE dim_geography (
        geography_id INTEGER PRIMARY KEY, country_code VARCHAR, region VARCHAR)""")
    for i, (cc, rg) in enumerate(GEOS, 1):
        con.execute("INSERT INTO dim_geography VALUES (?,?,?)", [i, cc, rg])

    con.execute("""CREATE TABLE dim_forecast_cycle (
        cycle_id INTEGER PRIMARY KEY, cycle_code VARCHAR, published_date DATE)""")
    for i, (code, d) in enumerate(CYCLES, 1):
        con.execute("INSERT INTO dim_forecast_cycle VALUES (?,?,?)", [i, code, d])

    # ---- raw layer (S3 shape: stringly-typed, shipment grain) ----
    con.execute("""CREATE TABLE raw_s3_gross_sales (
        file_name VARCHAR, row_num INTEGER, brand_name VARCHAR, country VARCHAR,
        ship_month VARCHAR, units VARCHAR, gross_amount VARCHAR,
        gtn_adjustment VARCHAR, load_ts TIMESTAMP)""")
    con.execute("""CREATE TABLE raw_s3_forecast (
        file_name VARCHAR, row_num INTEGER, cycle_code VARCHAR, brand_name VARCHAR,
        country VARCHAR, fcst_month VARCHAR, fcst_units VARCHAR,
        fcst_net_revenue VARCHAR, load_ts TIMESTAMP)""")

    raw_rows, dup_budget = [], 3
    for (b, ta, price) in BRANDS:
        base = rng.uniform(8_000, 60_000)
        for gi, (cc, rg) in enumerate(GEOS, 1):
            geo_f = {"US": 1.0, "DE": 0.35, "FR": 0.3, "JP": 0.4, "UK": 0.28}[cc]
            for mi, m in enumerate(MONTHS):
                trend = 1 + 0.012 * mi if b != "Veklury" else max(0.2, 1 - 0.03 * mi)
                units = base * geo_f * trend * rng.uniform(0.92, 1.08)
                gross = units * price
                gtn = gross * rng.uniform(0.18, 0.34)
                u = "" if (b == "Veklury" and cc == "JP" and mi == 5) else f"{units:.0f}"
                raw_rows.append([f"s3://gilead-comm-raw/sales/{m:%Y%m}.csv",
                                 len(raw_rows) + 1, b, cc, f"{m:%Y-%m}",
                                 u, f"{gross:.2f}", f"{gtn:.2f}"])
                if dup_budget and rng.random() < 0.004:
                    raw_rows.append(raw_rows[-1][:1] + [len(raw_rows) + 1] +
                                    raw_rows[-1][2:])
                    dup_budget -= 1
    while dup_budget:  # guarantee the documented duplicates exist
        raw_rows.append(raw_rows[dup_budget * 7][:1] + [len(raw_rows) + 1] +
                        raw_rows[dup_budget * 7][2:])
        dup_budget -= 1
    con.executemany(
        "INSERT INTO raw_s3_gross_sales VALUES (?,?,?,?,?,?,?,?, now())", raw_rows)

    fc_rows = []
    for (code, pub) in CYCLES:
        for (b, ta, price) in BRANDS:
            for (cc, rg) in GEOS:
                for m in MONTHS[12:]:
                    if (code == "FC-2026-Q2" and b == "Epclusa" and cc == "UK"
                            and m == date(2026, 8, 1)):
                        continue  # imperfection 4: late-arriving forecast
                    f_units = rng.uniform(0.85, 1.15)
                    base = rng.uniform(8_000, 60_000) * \
                        {"US": 1.0, "DE": 0.35, "FR": 0.3, "JP": 0.4, "UK": 0.28}[cc]
                    net = base * price * rng.uniform(0.66, 0.82) * f_units
                    fc_rows.append([f"s3://gilead-comm-raw/forecast/{code}.csv",
                                    len(fc_rows) + 1, code, b, cc, f"{m:%Y-%m}",
                                    f"{base * f_units:.0f}", f"{net:.2f}"])
    con.executemany(
        "INSERT INTO raw_s3_forecast VALUES (?,?,?,?,?,?,?,?, now())", fc_rows)

    # ---- staging: typed, deduped, conformed ----
    con.execute("""CREATE TABLE stg_sales_conformed AS
        SELECT DISTINCT ON (brand_name, country, ship_month)
            p.product_id, p.brand, g.geography_id,
            CAST(r.ship_month || '-01' AS DATE) AS month_start,
            TRY_CAST(r.units AS DOUBLE) AS units,
            TRY_CAST(r.gross_amount AS DOUBLE) AS gross_revenue,
            TRY_CAST(r.gtn_adjustment AS DOUBLE) AS gtn_adjustment,
            TRY_CAST(r.gross_amount AS DOUBLE)
              - TRY_CAST(r.gtn_adjustment AS DOUBLE) AS net_revenue
        FROM raw_s3_gross_sales r
        JOIN dim_product p ON p.brand = r.brand_name
        JOIN dim_geography g ON g.country_code = r.country""")

    con.execute("""CREATE TABLE stg_forecast_conformed AS
        SELECT c.cycle_id, c.cycle_code, p.product_id, p.brand, g.geography_id,
            CAST(r.fcst_month || '-01' AS DATE) AS month_start,
            TRY_CAST(r.fcst_units AS DOUBLE) AS forecast_units,
            TRY_CAST(r.fcst_net_revenue AS DOUBLE) AS forecast_net_revenue
        FROM raw_s3_forecast r
        JOIN dim_forecast_cycle c ON c.cycle_code = r.cycle_code
        JOIN dim_product p ON p.brand = r.brand_name
        JOIN dim_geography g ON g.country_code = r.country""")

    # ---- facts ----
    con.execute("""CREATE TABLE fct_revenue_actual AS
        SELECT product_id, brand, geography_id, month_start, units,
               gross_revenue, gtn_adjustment, net_revenue
        FROM stg_sales_conformed""")
    con.execute("""CREATE TABLE fct_revenue_forecast AS
        SELECT cycle_id, cycle_code, product_id, brand, geography_id,
               month_start, forecast_units, forecast_net_revenue
        FROM stg_forecast_conformed""")

    # ---- mart: actual vs forecast, latest cycle per month ----
    con.execute("""CREATE TABLE mart_revenue_actual_vs_forecast AS
        WITH latest AS (
            SELECT f.*, ROW_NUMBER() OVER (
                PARTITION BY f.product_id, f.geography_id, f.month_start
                ORDER BY c.published_date DESC) AS rn
            FROM fct_revenue_forecast f
            JOIN dim_forecast_cycle c ON c.cycle_id = f.cycle_id)
        SELECT a.brand, p.therapeutic_area, a.geography_id, g.country_code,
               g.region, a.month_start,
               a.units AS actual_units, a.net_revenue AS actual_revenue,
               l.cycle_code AS forecast_cycle,
               l.forecast_units, l.forecast_net_revenue AS forecast_revenue,
               ROUND(a.net_revenue - COALESCE(l.forecast_net_revenue, 0), 2)
                   AS variance_amount,
               CASE WHEN COALESCE(l.forecast_net_revenue, 0) <> 0
                    THEN ROUND((a.net_revenue - l.forecast_net_revenue)
                               / l.forecast_net_revenue * 100, 2) END
                   AS variance_pct
        FROM fct_revenue_actual a
        JOIN dim_product p ON p.product_id = a.product_id
        JOIN dim_geography g ON g.geography_id = a.geography_id
        LEFT JOIN latest l ON l.rn = 1 AND l.product_id = a.product_id
             AND l.geography_id = a.geography_id AND l.month_start = a.month_start""")

    # imperfection 3: drift the variance on two Trodelvy rows
    con.execute("""UPDATE mart_revenue_actual_vs_forecast
        SET variance_amount = variance_amount + 0.02
        WHERE brand = 'Trodelvy' AND country_code = 'US'
          AND month_start IN (DATE '2026-05-01', DATE '2026-06-01')""")

    n = con.execute("SELECT COUNT(*) FROM mart_revenue_actual_vs_forecast").fetchone()[0]
    print(f"seeded {DB} — mart rows: {n}")
    for t in ["raw_s3_gross_sales", "raw_s3_forecast", "fct_revenue_actual",
              "fct_revenue_forecast"]:
        print(f"  {t}: {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]} rows")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", default=True)
    main(ap.parse_args().force)
