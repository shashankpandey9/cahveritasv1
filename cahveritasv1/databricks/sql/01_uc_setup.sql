-- CAH VERITAS — Unity Catalog setup (run on a SQL warehouse)
CREATE CATALOG IF NOT EXISTS cah;
CREATE SCHEMA IF NOT EXISTS cah.veritas;
CREATE VOLUME IF NOT EXISTS cah.veritas.inputs;   -- drop BRD/design/sources/code here
CREATE VOLUME IF NOT EXISTS cah.veritas.state;    -- app DuckDB state
CREATE VOLUME IF NOT EXISTS cah.veritas.kb;       -- affirmed markdown KB

-- Analytics store: mirror of the local seed (populate from real pipelines,
-- or adapt data/seed_mart_db.py for synthetic UAT rehearsal data).
CREATE TABLE IF NOT EXISTS cah.veritas.dim_product (
  product_id INT, brand STRING, therapeutic_area STRING, wac_unit_price DOUBLE);
CREATE TABLE IF NOT EXISTS cah.veritas.dim_geography (
  geography_id INT, country_code STRING, region STRING);
CREATE TABLE IF NOT EXISTS cah.veritas.dim_forecast_cycle (
  cycle_id INT, cycle_code STRING, published_date DATE);
CREATE TABLE IF NOT EXISTS cah.veritas.fct_revenue_actual (
  product_id INT, brand STRING, geography_id INT, month_start DATE,
  units DOUBLE, gross_revenue DOUBLE, gtn_adjustment DOUBLE, net_revenue DOUBLE);
CREATE TABLE IF NOT EXISTS cah.veritas.fct_revenue_forecast (
  cycle_id INT, cycle_code STRING, product_id INT, brand STRING,
  geography_id INT, month_start DATE, forecast_units DOUBLE,
  forecast_net_revenue DOUBLE);
CREATE TABLE IF NOT EXISTS cah.veritas.mart_revenue_actual_vs_forecast (
  brand STRING, therapeutic_area STRING, geography_id INT, country_code STRING,
  region STRING, month_start DATE, actual_units DOUBLE, actual_revenue DOUBLE,
  forecast_cycle STRING, forecast_units DOUBLE, forecast_revenue DOUBLE,
  variance_amount DOUBLE, variance_pct DOUBLE);

-- Corpus chunks Delta table (delta-sync source for Vector Search)
CREATE TABLE IF NOT EXISTS cah.veritas.corpus_chunks (
  chunk_id BIGINT GENERATED ALWAYS AS IDENTITY,
  doc_name STRING, doc_type STRING, section STRING, ord INT, text STRING
) TBLPROPERTIES (delta.enableChangeDataFeed = true);
