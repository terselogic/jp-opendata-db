"""Tests verifying grid-bucketed nearest land-price lookup matches brute force."""

from __future__ import annotations

import math

import polars as pl

from jp_opendata.sources.mlit_gis import attach_nearest_land_price_fast


def test_grid_nearest_matches_brute_force() -> None:
    corps = pl.DataFrame(
        [
            {"corporate_number": "1000000000001", "latitude": 35.6812, "longitude": 139.7671},
            {"corporate_number": "1000000000002", "latitude": 35.6595, "longitude": 139.7004},
            # 遠方フォールバック（隣接0.05度グリッド外）
            {"corporate_number": "1000000000003", "latitude": 35.9000, "longitude": 139.9500},
        ]
    )
    lps = pl.DataFrame(
        [
            {"price_yen_sqm": 25000000, "latitude": 35.6810, "longitude": 139.7670},
            {"price_yen_sqm": 12000000, "latitude": 35.6600, "longitude": 139.7010},
            {"price_yen_sqm": 500000, "latitude": 35.7200, "longitude": 139.7100},
        ]
    )

    fast = attach_nearest_land_price_fast(corps, lps).to_dicts()

    lp_list = lps.to_dicts()
    for row in fast:
        best_p = None
        best_d = float("inf")
        for lp in lp_list:
            d = math.sqrt(
                ((row["latitude"] - lp["latitude"]) * 111000.0) ** 2
                + ((row["longitude"] - lp["longitude"]) * 91000.0) ** 2
            )
            if d < best_d:
                best_d = d
                best_p = lp["price_yen_sqm"]
        assert row["nearest_land_price_yen_sqm"] == best_p
        assert row["nearest_land_price_dist_m"] == round(best_d, 2)
