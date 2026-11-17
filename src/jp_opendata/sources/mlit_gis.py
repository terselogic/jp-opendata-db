"""Spatial enrichment using DuckDB Spatial extension and MLIT GIS layers."""

from __future__ import annotations

import math
import tempfile

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


def attach_nearest_land_price_fast(
    geo_corps: pl.DataFrame,
    land_price_points_df: pl.DataFrame,
) -> pl.DataFrame:
    """Find nearest land price point for 1M+ corporates via 0.05-deg spatial grid bucketing."""
    lp_rows = land_price_points_df.select(["price_yen_sqm", "latitude", "longitude"]).to_dicts()
    if not lp_rows:
        return geo_corps.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("nearest_land_price_yen_sqm"),
            pl.lit(None, dtype=pl.Float64).alias("nearest_land_price_dist_m"),
        )

    cell_deg = 0.05
    grid: dict[tuple[int, int], list[tuple[float, float, int]]] = {}
    all_points: list[tuple[float, float, int]] = []
    for r in lp_rows:
        lat, lon, price = float(r["latitude"]), float(r["longitude"]), int(r["price_yen_sqm"])
        pt = (lat, lon, price)
        all_points.append(pt)
        gx, gy = math.floor(lat / cell_deg), math.floor(lon / cell_deg)
        grid.setdefault((gx, gy), []).append(pt)

    prices: list[int | None] = []
    dists: list[float | None] = []
    for row in geo_corps.select(["latitude", "longitude"]).to_dicts():
        clat, clon = float(row["latitude"]), float(row["longitude"])
        gx, gy = math.floor(clat / cell_deg), math.floor(clon / cell_deg)
        cands: list[tuple[float, float, int]] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cands.extend(grid.get((gx + dx, gy + dy), ()))
        if not cands:
            cands = all_points

        best_d2 = float("inf")
        best_price: int | None = None
        for plat, plon, price in cands:
            dlat = (clat - plat) * 111000.0
            dlon = (clon - plon) * 91000.0
            d2 = dlat * dlat + dlon * dlon
            if d2 < best_d2:
                best_d2 = d2
                best_price = price

        prices.append(best_price)
        dists.append(round(math.sqrt(best_d2), 2) if best_price is not None else None)

    return geo_corps.with_columns(
        pl.Series("nearest_land_price_yen_sqm", prices, dtype=pl.Int64),
        pl.Series("nearest_land_price_dist_m", dists, dtype=pl.Float64),
    )


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

    has_polygons = (
        (zoning_polygons_df is not None and not zoning_polygons_df.is_empty())
        or (hazard_polygons_df is not None and not hazard_polygons_df.is_empty())
    )

    if not has_polygons:
        base_df = (
            attach_nearest_land_price_fast(geo_corps, land_price_points_df)
            if land_price_points_df is not None and not land_price_points_df.is_empty()
            else geo_corps.with_columns(
                pl.lit(None, dtype=pl.Int64).alias("nearest_land_price_yen_sqm"),
                pl.lit(None, dtype=pl.Float64).alias("nearest_land_price_dist_m"),
            )
        )
        return (
            base_df.with_columns(
                pl.lit(None, dtype=pl.String).alias("zoning_name"),
                pl.lit(None, dtype=pl.String).alias("flood_hazard_rank"),
            )
            .select(list(SPATIAL_SCHEMA.keys()))
            .sort("corporate_number")
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        corps_pq = f"{tmpdir}/corps.parquet"
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
                zp_pq = f"{tmpdir}/zoning.parquet"
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
                hz_pq = f"{tmpdir}/hazard.parquet"
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

            out_pq = f"{tmpdir}/poly_out.parquet"
            con.execute(
                f"""
                COPY (
                    SELECT c.corporate_number, c.latitude, c.longitude,
                           {zoning_sql}, {hazard_sql}
                    FROM corps c
                    {zoning_join}
                    {hazard_join}
                    ORDER BY c.corporate_number
                ) TO '{out_pq}' (FORMAT PARQUET)
                """
            )
            poly_df = pl.read_parquet(out_pq)
        finally:
            con.close()

    if land_price_points_df is not None and not land_price_points_df.is_empty():
        lp_df = attach_nearest_land_price_fast(
            poly_df.select(["corporate_number", "latitude", "longitude"]),
            land_price_points_df,
        ).select(["corporate_number", "nearest_land_price_yen_sqm", "nearest_land_price_dist_m"])
        poly_df = poly_df.join(lp_df, on="corporate_number", how="left")
    else:
        poly_df = poly_df.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("nearest_land_price_yen_sqm"),
            pl.lit(None, dtype=pl.Float64).alias("nearest_land_price_dist_m"),
        )

    return poly_df.select(list(SPATIAL_SCHEMA.keys())).cast(SPATIAL_SCHEMA)
