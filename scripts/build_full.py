"""Production builder for jp-opendata-db full package (Phase 1: Tokyo full)."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import shutil
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
    PermitRecord,
    load_fsa_excel_permits,
    load_mhlw_kaigo_permits,
    resolve_permits_to_corporates,
)
from jp_opendata.validator import SOURCES_METADATA_SCHEMA

NTA_ZENKEN_URL = "https://www.houjin-bangou.nta.go.jp/pc/download/zenken/index.html"
NTA_POST_URL = "https://www.houjin-bangou.nta.go.jp/download/zenken/index.html"
MLIT_L01_TOKYO_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/L01/L01-25/L01-25_13_GML.zip"
MLIT_ISJ_GAIKU_TOKYO_URL = "https://nlftp.mlit.go.jp/isj/dls/data/24.0a/13000-24.0a.zip"
MLIT_ISJ_OAZA_TOKYO_URL = "https://nlftp.mlit.go.jp/isj/dls/data/18.0b/13000-18.0b.zip"
MHLW_OPENDATA_PAGE = "https://www.mhlw.go.jp/stf/kaigo-kouhyou_opendata.html"

FSA_EXCEL_TARGETS: tuple[tuple[str, str], ...] = (
    ("https://www.fsa.go.jp/menkyo/menkyoj/kinyushohin.xlsx", "金融商品取引業者"),
    ("https://www.fsa.go.jp/menkyo/menkyoj/kasi.xlsx", "貸金業者（財務局長登録）"),
    ("https://www.fsa.go.jp/menkyo/menkyoj/shikin_idou.xlsx", "資金移動業者"),
    ("https://www.fsa.go.jp/menkyo/menkyoj/kasoutuka.xlsx", "暗号資産交換業者"),
    ("https://www.fsa.go.jp/menkyo/menkyoj/ginkou.xlsx", "銀行"),
    ("https://www.fsa.go.jp/menkyo/menkyoj/shinkin.xlsx", "信用金庫"),
    ("https://www.fsa.go.jp/menkyo/menkyoj/daisan.xlsx", "前払式支払手段（第三者型）"),
    ("https://www.fsa.go.jp/menkyo/menkyoj/kansahoujin.xlsx", "監査法人"),
)


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
        idx = page.text.find("東京都")
        section = page.text[idx : idx + 1500]
        file_nos = re.findall(r"doDownload\((\d+)\)", section)
        outputs: list[Path] = []
        for dest, file_no in zip((part1, part2), file_nos[:2]):
            if dest.exists():
                outputs.append(dest)
                continue
            payload = {**hiddens, "selDlFileNo": file_no}
            resp = client.post(NTA_POST_URL, data=payload)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            outputs.append(dest)
        return outputs


def discover_mhlw_kaigo_csvs(limit: int = 8) -> list[str]:
    """Fetch service CSV URLs listed on the MHLW Care Service Open Data page."""
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        r = client.get(MHLW_OPENDATA_PAGE)
        r.raise_for_status()
    rels = re.findall(r'href=["\'](/content/[^"\']+/jigyosho_[^"\']+\.csv)["\']', r.text)
    seen: list[str] = []
    for rel in rels:
        url = f"https://www.mhlw.go.jp{rel}"
        if url not in seen:
            seen.append(url)
    return seen[:limit]


def parse_mlit_l01_gml(zip_path: Path) -> pl.DataFrame:
    """Parse MLIT L01 GML ZIP into (price_yen_sqm, latitude, longitude, municipality)."""
    with zipfile.ZipFile(zip_path) as zf:
        xml_names = [
            n
            for n in zf.namelist()
            if n.lower().endswith(".xml") and "KS-META" not in n.upper()
        ]
        raw_bytes = zf.read(xml_names[0])

    raw_xml = (
        raw_bytes.decode("utf-8", errors="replace")
        if b"UTF-8" in raw_bytes[:120].upper()
        else raw_bytes.decode("cp932", errors="replace")
    )
    raw_xml = re.sub(r'encoding=["\'][^"\']+["\']', 'encoding="utf-8"', raw_xml, count=1)

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
        if elem.tag.split("}")[-1] != "LandPrice":
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


def build_tokyo_full(root: Path, mhlw_limit: int = 8) -> Path:
    """Build full Tokyo integrated DB package and distributable ZIP + SHA-256."""
    raw_dir = root / "data" / "raw"
    out_dir = root / "data" / "out"
    dist_dir = root / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    nta_zips = download_nta_tokyo_zips(raw_dir)
    l01_zip = download_file(MLIT_L01_TOKYO_URL, raw_dir / "L01-25_13_GML.zip")
    isj_gaiku_zip = download_file(MLIT_ISJ_GAIKU_TOKYO_URL, raw_dir / "13000-24.0a.zip")
    isj_oaza_zip = download_file(MLIT_ISJ_OAZA_TOKYO_URL, raw_dir / "13000-18.0b.zip")

    print("Loading NTA Tokyo corporates (all split archives)...")
    corps_df = pl.concat([load_nta_corporates(z, limit=None) for z in nta_zips]).unique(
        subset=["corporate_number"], keep="first"
    )

    print("Geocoding ALL corporates with MLIT ISJ (gaiku 24.0a + oaza 18.0b)...")
    corps_geo = geocode_corporates_with_isj(corps_df, isj_gaiku_zip, isj_oaza_zip)
    geo_count = corps_geo.filter(pl.col("latitude").is_not_null()).height
    print(f"Geocoded: {geo_count:,} / {corps_geo.height:,} ({geo_count / corps_geo.height:.1%})")

    print("Parsing MLIT L01 Tokyo land prices & enriching ALL geocoded corporates...")
    l01_df = parse_mlit_l01_gml(l01_zip)
    full_spatial_df = enrich_corporates_spatial(
        corps_geo.filter(pl.col("latitude").is_not_null()),
        land_price_points_df=l01_df.select(["price_yen_sqm", "latitude", "longitude"]),
    )

    print(f"Downloading & loading MHLW Care Service CSVs (top {mhlw_limit} services)...")
    permit_records: list[PermitRecord] = []
    for url in discover_mhlw_kaigo_csvs(limit=mhlw_limit):
        fname = url.rsplit("/", 1)[-1]
        csv_path = download_file(url, raw_dir / fname)
        permit_records.extend(load_mhlw_kaigo_permits(csv_path, prefecture_filter="東京都"))

    print("Downloading & loading FSA licensed-operator Excel lists...")
    for url, category in FSA_EXCEL_TARGETS:
        fname = "fsa_" + url.rsplit("/", 1)[-1]
        xlsx_path = download_file(url, raw_dir / fname)
        permit_records.extend(
            load_fsa_excel_permits(xlsx_path, category, prefecture_filter="東京都")
        )

    # 重複permit_idを除去して名寄せ
    deduped_permits = list({p.permit_id: p for p in permit_records}.values())
    permits_df = resolve_permits_to_corporates(deduped_permits, corps_geo)

    edinet_parquet = root.parent / "edinet-pit-dataset" / "data" / "out" / "edinet_pit_cohort.parquet"
    edinet_df = build_edinet_bridge(edinet_parquet, corps_geo)

    sources_df = pl.DataFrame(
        [
            {
                "source_id": "nta_corp",
                "source_name": "国税庁法人番号公表サイト（東京都）",
                "publisher": "国税庁",
                "license_name": "公共データ利用規約（第1.0版）",
                "source_url": NTA_ZENKEN_URL,
                "retrieved_date": "2026-11-18",
            },
            {
                "source_id": "mlit_isj",
                "source_name": "国土交通省 位置参照情報（街区24.0a・大字町丁目18.0b 東京都）",
                "publisher": "国土交通省",
                "license_name": "位置参照情報利用約款（商用・有償二次配布可）",
                "source_url": "https://nlftp.mlit.go.jp/isj/",
                "retrieved_date": "2026-11-18",
            },
            {
                "source_id": "mlit_l01",
                "source_name": "国土数値情報 地価公示データ（東京都 L01-25）",
                "publisher": "国土交通省",
                "license_name": "国土数値情報利用約款（L01商用可）",
                "source_url": MLIT_L01_TOKYO_URL,
                "retrieved_date": "2026-11-18",
            },
            {
                "source_id": "mhlw_kaigo",
                "source_name": "介護サービス情報公表システム オープンデータ（複数サービス種別）",
                "publisher": "厚生労働省",
                "license_name": "CC BY 4.0 / 厚生労働省利用規約",
                "source_url": MHLW_OPENDATA_PAGE,
                "retrieved_date": "2026-11-18",
            },
            {
                "source_id": "fsa_menkyo",
                "source_name": "免許・許可・登録等を受けている業者一覧（金融庁Excel一括）",
                "publisher": "金融庁",
                "license_name": "政府標準利用規約（第2.0版）準拠",
                "source_url": "https://www.fsa.go.jp/menkyo/menkyo.html",
                "retrieved_date": "2026-11-18",
            },
        ],
        schema=SOURCES_METADATA_SCHEMA,
    )

    pkg_dir = out_dir / "jp_opendata_tokyo_full_v1"
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    paths, report = export_integrated_db(
        output_dir=pkg_dir,
        corporates_df=corps_geo,
        permits_df=permits_df,
        spatial_df=full_spatial_df,
        edinet_df=edinet_df,
        sources_df=sources_df,
        name="jp_opendata_tokyo_full",
    )

    attribution_txt = "\n".join(
        [
            "jp-opendata-db Tokyo Full Package (v1)",
            "All datasets included comply with commercial redistribution terms:",
            "- 国税庁法人番号公表サイト（東京都）: 公共データ利用規約（第1.0版）",
            "- 国土交通省 位置参照情報（24.0a / 18.0b）: 位置参照情報利用約款（出典明記で有償二次配布可）",
            "- 国土交通省 国土数値情報 地価公示（L01-25）: 国土数値情報利用約款（L01商用可）",
            "- 厚生労働省 介護サービス情報公表システム オープンデータ: CC BY 4.0",
            "- 金融庁 免許・許可・登録等を受けている業者一覧: 政府標準利用規約（第2.0版）準拠",
            "",
            report.to_markdown(),
        ]
    )
    (pkg_dir / "LICENSE_ATTRIBUTIONS.txt").write_text(attribution_txt + "\n", encoding="utf-8")

    zip_base = dist_dir / "jp_opendata_tokyo_full_v1"
    zip_path = Path(shutil.make_archive(str(zip_base), "zip", root_dir=pkg_dir))
    sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    (dist_dir / "jp_opendata_tokyo_full_v1.zip.sha256").write_text(
        f"{sha256}  {zip_path.name}\n", encoding="utf-8"
    )
    print(report.to_markdown())
    print(f"Distributable ZIP: {zip_path} ({zip_path.stat().st_size / (1024 * 1024):.1f} MB)")
    print(f"SHA-256: {sha256}")
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=["tokyo"], default="tokyo")
    parser.add_argument("--mhlw-limit", type=int, default=8)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    if args.scope == "tokyo":
        build_tokyo_full(root, mhlw_limit=args.mhlw_limit)


if __name__ == "__main__":
    main()
