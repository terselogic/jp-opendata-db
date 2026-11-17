"""Spatial enrichment using DuckDB Spatial extension and MLIT GIS layers."""

from __future__ import annotations

import duckdb
import polars as pl

SPATIAL_SCHEMA: dict[str, pl.DataType] = {
    "corporate_number": pl.String,
    "latitude": pl.Float64,
    "longitude": pl.Float64,
    "zoning_name": pl.String,
    "flood_hazard_rank": pl.String,
    "nearest_land_price_yen_sqm": pl.Int64,
    "nearest_land_price_dist_m": pl.Float64,
}


def enrich_corporates_spatial(
    corporates_df: pl.DataFrame,
    hazard_polygons_df: pl.DataFrame | None = None,
    zoning_polygons_df: pl.DataFrame | None = None,
    land_price_points_df: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Enrich coordinate-bearing corporates with MLIT zoning, hazard, and nearest land price."""
    geo_corps = corporates_df.filter(
        pl.col("latitude").is_not_null() & pl.col("longitude").is_not_null()
    ).select(["corporate_number", "latitude", "longitude"])

    if geo_corps.is_empty():
        return pl.DataFrame(schema=SPATIAL_SCHEMA)

    # DuckDB 2.0 + pyarrow無し環境でも確実に動作するよう一時Parquet経由で読み込む
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = tmpdir
        corps_pq = f"{tmp}/corps.parquet"
        geo_corps.write_parquet(corps_pq)

        con = duckdb.connect(":memory:")
        try:
            con.execute("LOAD spatial;")
            con.execute(
                f"""
                CREATE TABLE corps AS
                SELECT corporate_number, latitude, longitude,
                       ST_Point(longitude, latitude) AS geom
                FROM read_parquet('{corps_pq}')
                """
            )

            zoning_sql = "NULL::VARCHAR AS zoning_name"
            zoning_join = ""
            if zoning_polygons_df is not None and not zoning_polygons_df.is_empty():
                zp_pq = f"{tmp}/zoning.parquet"
                zoning_polygons_df.write_parquet(zp_pq)
                con.execute(
                    f"""
                    CREATE TABLE zoning AS
                    SELECT zoning_name, ST_GeomFromText(wkt) AS geom
                    FROM read_parquet('{zp_pq}')
                    """
                )
                zoning_sql = "z.zoning_name"
                zoning_join = "LEFT JOIN zoning z ON ST_Contains(z.geom, c.geom)"

            hazard_sql = "NULL::VARCHAR AS flood_hazard_rank"
            hazard_join = ""
            if hazard_polygons_df is not None and not hazard_polygons_df.is_empty():
                hz_pq = f"{tmp}/hazard.parquet"
                hazard_polygons_df.write_parquet(hz_pq)
                con.execute(
                    f"""
                    CREATE TABLE hazard AS
                    SELECT flood_hazard_rank, ST_GeomFromText(wkt) AS geom
                    FROM read_parquet('{hz_pq}')
                    """
                )
                hazard_sql = "h.flood_hazard_rank"
                hazard_join = "LEFT JOIN hazard h ON ST_Contains(h.geom, c.geom)"

            if land_price_points_df is not None and not land_price_points_df.is_empty():
                lp_pq = f"{tmp}/land_price.parquet"
                land_price_points_df.write_parquet(lp_pq)
                con.execute(
                    f"""
                    CREATE TABLE land_price AS
                    SELECT price_yen_sqm,
                           latitude AS lp_lat,
                           longitude AS lp_lon
                    FROM read_parquet('{lp_pq}')
                    """
                )
                # Haversine近似（緯度35度付近のメートル換算）で最寄り地価公示点を取得
                query = f"""
                WITH base AS (
                    SELECT c.corporate_number,
                           c.latitude,
                           c.longitude,
                           {zoning_sql},
                           {hazard_sql}
                    FROM corps c
                    {zoning_join}
                    {hazard_join}
                ),
                ranked_lp AS (
                    SELECT b.*,
                           lp.price_yen_sqm AS nearest_land_price_yen_sqm,
                           SQRT(
                               POW((b.latitude - lp.lp_lat) * 111000.0, 2) +
                               POW((b.longitude - lp.lp_lon) * 91000.0, 2)
                           ) AS nearest_land_price_dist_m,
                           ROW_NUMBER() OVER (
                               PARTITION BY b.corporate_number
                               ORDER BY POW(b.latitude - lp.lp_lat, 2) + POW(b.longitude - lp.lp_lon, 2)
                           ) AS rn
                    FROM base b
                    CROSS JOIN land_price lp
                )
                SELECT corporate_number,
                       latitude,
                       longitude,
                       zoning_name,
                       flood_hazard_rank,
                       CAST(nearest_land_price_yen_sqm AS BIGINT) AS nearest_land_price_yen_sqm,
                       ROUND(nearest_land_price_dist_m, 2) AS nearest_land_price_dist_m
                FROM ranked_lp
                WHERE rn = 1
                ORDER BY corporate_number
                """
            else:
                query = f"""
                SELECT c.corporate_number,
                       c.latitude,
                       c.longitude,
                       {zoning_sql},
                       {hazard_sql},
                       NULL::BIGINT AS nearest_land_price_yen_sqm,
                       NULL::DOUBLE AS nearest_land_price_dist_m
                FROM corps c
                {zoning_join}
                {hazard_join}
                ORDER BY c.corporate_number
                """

            out_pq = f"{tmp}/spatial_out.parquet"
            con.execute(f"COPY ({query}) TO '{out_pq}' (FORMAT PARQUET)")
            return pl.read_parquet(out_pq).cast(SPATIAL_SCHEMA)
        finally:
            con.close()
