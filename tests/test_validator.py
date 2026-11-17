"""Unit tests for entity resolution, spatial enrichment, validation, and export."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import polars as pl
import pytest

from jp_opendata.exporter import export_integrated_db
from jp_opendata.normalizer import compute_corporate_number_check_digit
from jp_opendata.sources.mlit_gis import enrich_corporates_spatial
from jp_opendata.sources.nta_corp import build_corporates_from_records
from jp_opendata.sources.permits import PermitRecord, resolve_permits_to_corporates
from jp_opendata.validator import SOURCES_METADATA_SCHEMA, validate_dataset


def _make_corp_num(body_12: str) -> str:
    return compute_corporate_number_check_digit(body_12) + body_12


def _sample_sources() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "source_id": "nta_corp",
                "source_name": "国税庁法人番号公表サイト",
                "publisher": "国税庁",
                "license_name": "公共データ利用規約（第1.0版）",
                "source_url": "https://www.houjin-bangou.nta.go.jp/",
                "retrieved_date": "2026-11-17",
            }
        ],
        schema=SOURCES_METADATA_SCHEMA,
    )


def test_multi_tier_permit_resolution_and_ambiguity_guard() -> None:
    c1 = _make_corp_num("100000000001")
    c2 = _make_corp_num("100000000002")
    c3 = _make_corp_num("100000000003")

    corps = build_corporates_from_records(
        [
            {
                "corporate_number": c1,
                "company_name": "株式会社高橋建設",
                "raw_address": "東京都新宿区西新宿二丁目8番1号",
                "latitude": 35.6896,
                "longitude": 139.6917,
            },
            # 同一市区町村に同名社が2社ある曖昧ケース
            {
                "corporate_number": c2,
                "company_name": "株式会社鈴木商事",
                "raw_address": "東京都渋谷区渋谷一丁目1番1号",
            },
            {
                "corporate_number": c3,
                "company_name": "鈴木商事株式会社",
                "raw_address": "東京都渋谷区宇田川町二丁目2番2号",
            },
        ]
    )

    permits = [
        PermitRecord(
            permit_id="P-001",
            permit_category="建設業",
            authority_name="東京都",
            raw_company_name="㈱髙橋建設",
            raw_address="東京都新宿区西新宿2-8-1 新宿ビル",
        ),
        PermitRecord(
            permit_id="P-002",
            permit_category="建設業",
            authority_name="東京都",
            raw_company_name="鈴木商事",
            raw_address="東京都渋谷区恵比寿3-3-3",
        ),
    ]

    resolved = resolve_permits_to_corporates(permits, corps)
    rows = {r["permit_id"]: r for r in resolved.to_dicts()}

    assert rows["P-001"]["corporate_number"] == c1
    assert rows["P-001"]["match_tier"] == "tier2_exact_name_street"
    assert rows["P-002"]["corporate_number"] is None
    assert rows["P-002"]["match_tier"] == "ambiguous"


def test_spatial_enrichment_and_export_sqlite_duckdb(tmp_path: Path) -> None:
    c1 = _make_corp_num("200000000001")
    corps = build_corporates_from_records(
        [
            {
                "corporate_number": c1,
                "company_name": "株式会社丸の内不動産",
                "raw_address": "東京都千代田区丸の内一丁目1番1号",
                "latitude": 35.6812,
                "longitude": 139.7671,
            }
        ]
    )

    zoning_df = pl.DataFrame(
        [
            {
                "zoning_name": "商業地域",
                "wkt": "POLYGON((139.75 35.67, 139.78 35.67, 139.78 35.69, 139.75 35.69, 139.75 35.67))",
            }
        ]
    )
    hazard_df = pl.DataFrame(
        [
            {
                "flood_hazard_rank": "0.5m〜3.0m未満",
                "wkt": "POLYGON((139.75 35.67, 139.78 35.67, 139.78 35.69, 139.75 35.69, 139.75 35.67))",
            }
        ]
    )
    land_price_df = pl.DataFrame(
        [{"price_yen_sqm": 25000000, "latitude": 35.6810, "longitude": 139.7670}]
    )

    spatial = enrich_corporates_spatial(
        corps,
        hazard_polygons_df=hazard_df,
        zoning_polygons_df=zoning_df,
        land_price_points_df=land_price_df,
    )
    assert spatial.height == 1
    assert spatial[0, "zoning_name"] == "商業地域"
    assert spatial[0, "flood_hazard_rank"] == "0.5m〜3.0m未満"
    assert spatial[0, "nearest_land_price_yen_sqm"] == 25000000

    permits = resolve_permits_to_corporates(
        [
            PermitRecord(
                permit_id="RE-01",
                permit_category="宅地建物取引業",
                authority_name="国土交通省",
                raw_company_name="丸の内不動産株式会社",
                raw_address="東京都千代田区丸の内1-1-1",
                corporate_number=c1,
            )
        ],
        corps,
    )
    edinet_empty = pl.DataFrame(
        schema={
            "corporate_number": pl.String,
            "edinet_code": pl.String,
            "sec_code": pl.String,
            "filer_name": pl.String,
            "latest_period_end": pl.Date,
            "net_sales": pl.Float64,
            "operating_income": pl.Float64,
            "net_assets": pl.Float64,
            "risk_change_score": pl.Float64,
        }
    )

    paths, report = export_integrated_db(
        output_dir=tmp_path,
        corporates_df=corps,
        permits_df=permits,
        spatial_df=spatial,
        edinet_df=edinet_empty,
        sources_df=_sample_sources(),
        name="test_mvp",
    )

    assert report.permits_match_rate == 1.0
    assert paths["sqlite"].exists()
    assert paths["duckdb"].exists()

    scon = sqlite3.connect(str(paths["sqlite"]))
    try:
        joined = scon.execute(
            """
            SELECT c.company_name, p.permit_category, s.zoning_name, s.nearest_land_price_yen_sqm
            FROM corporates c
            JOIN permits p ON c.corporate_number = p.corporate_number
            JOIN spatial_profiles s ON c.corporate_number = s.corporate_number
            """
        ).fetchall()
        assert joined == [("株式会社丸の内不動産", "宅地建物取引業", "商業地域", 25000000)]
    finally:
        scon.close()


def test_validator_rejects_pii_and_fk_violations() -> None:
    c1 = _make_corp_num("300000000001")
    corps = build_corporates_from_records(
        [
            {
                "corporate_number": c1,
                "company_name": "株式会社テスト",
                "raw_address": "東京都港区芝1-1-1",
            }
        ]
    )
    bad_permits = pl.DataFrame(
        [
            {
                "permit_id": "X-1",
                "permit_category": "テスト",
                "authority_name": "東京都",
                "raw_company_name": "株式会社テスト 090-1234-5678",
                "raw_address": "東京都港区芝1-1-1",
                "corporate_number": c1,
                "match_tier": "tier1_explicit_corp_num",
                "match_confidence": 1.0,
                "latitude": None,
                "longitude": None,
            }
        ]
    )
    empty_spatial = pl.DataFrame(schema={"corporate_number": pl.String})
    empty_edinet = pl.DataFrame(schema={"corporate_number": pl.String})

    with pytest.raises(ValueError, match="Potential PII"):
        validate_dataset(corps, bad_permits, empty_spatial, empty_edinet, _sample_sources())
