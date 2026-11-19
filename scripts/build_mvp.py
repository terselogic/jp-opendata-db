"""Build an integrated Japanese public open-data sample & Tokyo MVP DB.

All included datasets have verified commercial redistribution rights:
- NTA Corporate Number (PDL 1.0)
- MLIT National Spatial Reference ISJ (Terms Q20/Q21 commercial bundle allowed)
- MLIT National Land Numerical Information L01 Land Prices (commercial allowed)
- MHLW Care Service Open Data (CC BY)
- FSA Licensed Financial Operators Excel (Government Standard Terms 2.0)
"""

from __future__ import annotations

from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

import httpx
import polars as pl

from jp_opendata.exporter import export_integrated_db
from jp_opendata.normalizer import normalize_address
from jp_opendata.sources.edinet_bridge import build_edinet_bridge
from jp_opendata.sources.mlit_gis import enrich_corporates_spatial
from jp_opendata.sources.mlit_isj import geocode_corporates_with_isj
from jp_opendata.sources.nta_corp import load_nta_corporates
from jp_opendata.sources.permits import (
    load_fsa_excel_permits,
    load_mhlw_kaigo_permits,
    resolve_permits_to_corporates,
)
from jp_opendata.validator import SOURCES_METADATA_SCHEMA

NTA_ZENKEN_URL = "https://www.houjin-bangou.nta.go.jp/pc/download/zenken/index.html"
NTA_POST_URL = "https://www.houjin-bangou.nta.go.jp/download/zenken/index.html"
MLIT_L01_TOKYO_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/L01/L01-26/L01-26_13_GML.zip"
MLIT_ISJ_GAIKU_TOKYO_URL = "https://nlftp.mlit.go.jp/isj/dls/data/24.0a/13000-24.0a.zip"
MLIT_ISJ_OAZA_TOKYO_URL = "https://nlftp.mlit.go.jp/isj/dls/data/19.0b/13000-19.0b.zip"
MHLW_KAIGO_110_URL = "https://www.mhlw.go.jp/content/12300000/jigyosho_110_all_20260709180319.csv"
FSA_KINSHOU_URL = "https://www.fsa.go.jp/menkyo/menkyoj/kinyushohin.xlsx"
FSA_KASI_URL = "https://www.fsa.go.jp/menkyo/menkyoj/kasi.xlsx"
FSA_SHIKIN_IDOU_URL = "https://www.fsa.go.jp/menkyo/menkyoj/shikin_idou.xlsx"
FSA_GINKOU_URL = "https://www.fsa.go.jp/menkyo/menkyoj/ginkou.xlsx"
FSA_SHINKIN_URL = "https://www.fsa.go.jp/menkyo/menkyoj/shinkin.xlsx"


def download_nta_tokyo_zips(raw_dir: Path) -> list[Path]:
    """Download all split Unicode CSV ZIPs for Tokyo from NTA."""
    part1 = raw_dir / "nta_13_tokyo_part1.zip"
    part2 = raw_dir / "nta_13_tokyo_part2.zip"
    if part1.exists() and part2.exists():
        return [part1, part2]
    raw_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=180.0, follow_redirects=True) as client:
        page = client.get(NTA_ZENKEN_URL)
        page.raise_for_status()
        hiddens = dict(
            re.findall(
                r'<input[^>]+type=["\']hidden["\'][^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']*)["\']',
                page.text,
            )
        )
        # 東京都の Unicode ブロック全体から分割1・分割2の fileNo を抽出
        idx = page.text.find("東京都")
        if idx == -1:
            raise RuntimeError("Could not locate Tokyo section on NTA page.")
        section = page.text[idx : idx + 1500]
        file_nos = re.findall(r"doDownload\((\d+)\)", section)
        if len(file_nos) < 2:
            raise RuntimeError(f"Expected 2 split files for Tokyo, got: {file_nos}")

        outputs: list[Path] = []
        for dest, file_no in zip((part1, part2), file_nos[:2]):
            if dest.exists():
                outputs.append(dest)
                continue
            payload = {**hiddens, "selDlFileNo": file_no}
            resp = client.post(NTA_POST_URL, data=payload)
            resp.raise_for_status()
            if not resp.content.startswith(b"PK"):
                raise RuntimeError(
                    f"NTA download did not return a ZIP archive (fileNo={file_no})"
                )
            dest.write_bytes(resp.content)
            outputs.append(dest)
        return outputs


def download_file(url: str, dest: Path) -> Path:
    """Download a public file to dest if not already cached."""
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    return dest


def parse_mlit_l01_gml(zip_path: Path) -> pl.DataFrame:
    """Parse MLIT L01 (地価公示) GML ZIP into (price_yen_sqm, latitude, longitude, municipality)."""
    with zipfile.ZipFile(zip_path) as zf:
        xml_names = [
            n
            for n in zf.namelist()
            if n.lower().endswith(".xml") and "KS-META" not in n.upper()
        ]
        if not xml_names:
            raise FileNotFoundError(f"No data XML found in {zip_path}")
        raw_bytes = zf.read(xml_names[0])

    if b"UTF-8" in raw_bytes[:120].upper():
        raw_xml = raw_bytes.decode("utf-8", errors="replace")
    else:
        raw_xml = raw_bytes.decode("cp932", errors="replace")
    raw_xml = re.sub(
        r'encoding=["\'][^"\']+["\']', 'encoding="utf-8"', raw_xml, count=1
    )

    root = ET.fromstring(raw_xml)
    points: dict[str, tuple[float, float]] = {}
    records: list[dict[str, object]] = []

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "Point":
            gml_id = next(
                (v for k, v in elem.attrib.items() if k.endswith("}id") or k == "id"),
                "",
            )
            for child in elem:
                if child.tag.split("}")[-1] == "pos" and child.text:
                    parts = child.text.strip().split()
                    if len(parts) >= 2:
                        points[gml_id] = (float(parts[0]), float(parts[1]))

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag != "LandPrice":
            continue
        pos_ref = ""
        price = None
        addr_raw = ""
        for child in elem:
            ctag = child.tag.split("}")[-1]
            if ctag == "position":
                href = next(
                    (v for k, v in child.attrib.items() if k.endswith("}href") or k == "href"),
                    "",
                )
                pos_ref = href.lstrip("#")
            elif ctag == "postedLandPrice" and child.text:
                price = int(child.text.strip())
            elif ctag == "location" and child.text:
                addr_raw = child.text.strip()
        if pos_ref in points and price is not None:
            lat, lon = points[pos_ref]
            addr = normalize_address(addr_raw)
            records.append(
                {
                    "price_yen_sqm": price,
                    "latitude": lat,
                    "longitude": lon,
                    "municipality": addr.municipality,
                }
            )

    return pl.DataFrame(records)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    raw_dir = root / "data" / "raw"
    out_dir = root / "data" / "out"
    sample_dir = root / "sample"

    nta_zips = download_nta_tokyo_zips(raw_dir)
    l01_zip = download_file(MLIT_L01_TOKYO_URL, raw_dir / "L01-26_13_GML.zip")
    isj_gaiku_zip = download_file(MLIT_ISJ_GAIKU_TOKYO_URL, raw_dir / "13000-24.0a.zip")
    isj_oaza_zip = download_file(MLIT_ISJ_OAZA_TOKYO_URL, raw_dir / "13000-19.0b.zip")
    kaigo_csv = download_file(MHLW_KAIGO_110_URL, raw_dir / "mhlw_kaigo_110.csv")
    kinshou_xlsx = download_file(FSA_KINSHOU_URL, raw_dir / "fsa_kinyushohin.xlsx")
    kasi_xlsx = download_file(FSA_KASI_URL, raw_dir / "fsa_kasi.xlsx")
    shikin_xlsx = download_file(FSA_SHIKIN_IDOU_URL, raw_dir / "fsa_shikin_idou.xlsx")
    ginkou_xlsx = download_file(FSA_GINKOU_URL, raw_dir / "fsa_ginkou.xlsx")
    shinkin_xlsx = download_file(FSA_SHINKIN_URL, raw_dir / "fsa_shinkin.xlsx")

    print("Loading NTA Tokyo corporates (all split archives)...")
    corps_df = pl.concat([load_nta_corporates(z, limit=None) for z in nta_zips]).unique(
        subset=["corporate_number"], keep="first"
    )

    print("Geocoding corporates with MLIT ISJ (gaiku 24.0a + oaza 19.0b)...")
    corps_geo = geocode_corporates_with_isj(corps_df, isj_gaiku_zip, isj_oaza_zip)
    geocoded_count = corps_geo.filter(pl.col("latitude").is_not_null()).height
    print(f"Geocoded corporates: {geocoded_count:,} / {corps_geo.height:,} ({geocoded_count / corps_geo.height:.1%})")

    print("Parsing MLIT L01-26 Tokyo land prices...")
    l01_df = parse_mlit_l01_gml(l01_zip)

    print("Loading & resolving MHLW Care Service + FSA Financial Permits...")
    permit_records = (
        load_mhlw_kaigo_permits(kaigo_csv, prefecture_filter="東京都")
        + load_fsa_excel_permits(kinshou_xlsx, "金融商品取引業者", prefecture_filter="東京都")
        + load_fsa_excel_permits(kasi_xlsx, "貸金業者（財務局長登録）", prefecture_filter="東京都")
        + load_fsa_excel_permits(shikin_xlsx, "資金移動業者", prefecture_filter="東京都")
        + load_fsa_excel_permits(ginkou_xlsx, "銀行", prefecture_filter="東京都")
        + load_fsa_excel_permits(shinkin_xlsx, "信用金庫", prefecture_filter="東京都")
    )
    permits_df = resolve_permits_to_corporates(permit_records, corps_geo)

    edinet_parquet = root.parent / "edinet-pit-dataset" / "data" / "out" / "edinet_pit_cohort.parquet"
    edinet_df = build_edinet_bridge(edinet_parquet, corps_geo)

    # 許認可またはEDINETに紐付いた企業を優先して 2,000社のサンプルを選出
    matched_cnums = set(
        permits_df.filter(pl.col("corporate_number").is_not_null())["corporate_number"].to_list()
    )
    edinet_cnums = set(edinet_df["corporate_number"].to_list()) if not edinet_df.is_empty() else set()
    sample_corps = (
        corps_geo.filter(pl.col("latitude").is_not_null())
        .with_columns(
            pl.col("corporate_number").is_in(edinet_cnums).alias("_has_edinet"),
            pl.col("corporate_number").is_in(matched_cnums).alias("_has_permit"),
        )
        .sort(["_has_edinet", "_has_permit", "corporate_number"], descending=[True, True, False])
        .drop(["_has_edinet", "_has_permit"])
        .head(2000)
    )
    print(f"Running DuckDB Spatial nearest land-price enrichment ({sample_corps.height:,} sample corporates)...")
    spatial_df = enrich_corporates_spatial(
        sample_corps,
        land_price_points_df=l01_df.select(["price_yen_sqm", "latitude", "longitude"]),
    )

    sources_df = pl.DataFrame(
        [
            {
                "source_id": "nta_corp",
                "source_name": "国税庁法人番号公表サイト（東京都）を加工して作成",
                "publisher": "国税庁",
                "license_name": "公共データ利用規約（第1.0版 / PDL1.0）",
                "source_url": NTA_ZENKEN_URL,
                "retrieved_date": "2026-11-19",
            },
            {
                "source_id": "mlit_isj",
                "source_name": "国土交通省 位置参照情報（街区24.0a・大字町丁目19.0b 東京都）を加工して作成",
                "publisher": "国土交通省",
                "license_name": "位置参照情報利用約款（出典・加工表示の上で商用・有償二次配布可）",
                "source_url": "https://nlftp.mlit.go.jp/isj/",
                "retrieved_date": "2026-11-19",
            },
            {
                "source_id": "mlit_l01",
                "source_name": "国土数値情報 地価公示データ（東京都 L01-26 / 令和8年版）を加工して作成",
                "publisher": "国土交通省",
                "license_name": "国土数値情報利用約款（L01商用可）",
                "source_url": MLIT_L01_TOKYO_URL,
                "retrieved_date": "2026-11-19",
            },
            {
                "source_id": "mhlw_kaigo",
                "source_name": "介護サービス情報公表システム オープンデータ（訪問介護）を加工して作成",
                "publisher": "厚生労働省",
                "license_name": "CC BY 4.0 / 厚生労働省利用規約",
                "source_url": "https://www.mhlw.go.jp/stf/kaigo-kouhyou_opendata.html",
                "retrieved_date": "2026-11-19",
            },
            {
                "source_id": "fsa_menkyo",
                "source_name": "免許・許可・登録等を受けている業者一覧（金商・貸金・資金移動・銀行・信用金庫）を加工して作成",
                "publisher": "金融庁",
                "license_name": "公共データ利用規約（第1.0版 / PDL1.0）",
                "source_url": "https://www.fsa.go.jp/menkyo/menkyo.html",
                "retrieved_date": "2026-11-19",
            },
        ],
        schema=SOURCES_METADATA_SCHEMA,
    )

    sample_cnums = set(sample_corps["corporate_number"].to_list())
    edinet_sample = edinet_df.filter(pl.col("corporate_number").is_in(sample_cnums))
    permits_sample = permits_df.filter(
        pl.col("corporate_number").is_null() | pl.col("corporate_number").is_in(sample_cnums)
    )

    paths, sample_report = export_integrated_db(
        output_dir=sample_dir,
        corporates_df=sample_corps,
        permits_df=permits_sample,
        spatial_df=spatial_df,
        edinet_df=edinet_sample,
        sources_df=sources_df,
        name="jp_opendata_sample",
    )

    full_paths, full_report = export_integrated_db(
        output_dir=out_dir,
        corporates_df=corps_geo,
        permits_df=permits_df,
        spatial_df=spatial_df,
        edinet_df=edinet_df,
        sources_df=sources_df,
        name="jp_opendata_tokyo_mvp",
    )

    print(full_report.to_markdown())
    print("Sample SQLite:", paths["sqlite"])
    print("Full DuckDB:", full_paths["duckdb"])


if __name__ == "__main__":
    main()
