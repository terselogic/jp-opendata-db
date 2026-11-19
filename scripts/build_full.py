"""Production builder for jp-opendata-db full package (Tokyo / All-Japan 47 prefectures)."""

from __future__ import annotations

import argparse
import gc
import hashlib
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile

import httpx
import polars as pl

from jp_opendata.exporter import export_from_parquet_dir, export_integrated_db
from jp_opendata.normalizer import normalize_address, normalize_company_name
from jp_opendata.sources.edinet_bridge import (
    build_edinet_bridge,
    build_edinet_bridge_from_codelist,
)
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
MLIT_L01_TOKYO_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/L01/L01-26/L01-26_13_GML.zip"
MLIT_L01_ALL_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/L01/L01-26/L01-26_GML.zip"
MLIT_ISJ_GAIKU_VER = "24.0a"
MLIT_ISJ_OAZA_VER = "19.0b"
MLIT_ISJ_GAIKU_TOKYO_URL = f"https://nlftp.mlit.go.jp/isj/dls/data/{MLIT_ISJ_GAIKU_VER}/13000-{MLIT_ISJ_GAIKU_VER}.zip"
MLIT_ISJ_OAZA_TOKYO_URL = f"https://nlftp.mlit.go.jp/isj/dls/data/{MLIT_ISJ_OAZA_VER}/13000-{MLIT_ISJ_OAZA_VER}.zip"
MHLW_OPENDATA_PAGE = "https://www.mhlw.go.jp/stf/kaigo-kouhyou_opendata.html"
EDINET_CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
RETRIEVED_DATE = "2026-11-19"

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


def discover_mhlw_kaigo_csvs(limit: int | None = None) -> list[str]:
    """Fetch ALL latest-snapshot service CSV URLs listed on the MHLW Care Service Open Data page."""
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        r = client.get(MHLW_OPENDATA_PAGE)
        r.raise_for_status()
    matches = re.findall(
        r'href=["\'](/content/[^"\']+/jigyosho_(\d+)_all_(\d+)\.csv)["\']',
        r.text,
    )
    # 各サービスコードごとに最新タイムスタンプのCSVを選定（旧年度版との重複を防止）
    latest_by_code: dict[str, tuple[str, str]] = {}
    for rel, svc_code, ts in matches:
        url = f"https://www.mhlw.go.jp{rel}"
        prev = latest_by_code.get(svc_code)
        if prev is None or ts > prev[0]:
            latest_by_code[svc_code] = (ts, url)
    urls = [latest_by_code[k][1] for k in sorted(latest_by_code.keys())]
    return urls[:limit] if limit is not None else urls


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


def discover_nta_unicode_pref_files() -> tuple[dict[str, str], list[tuple[str, str, list[str]]]]:
    """Fetch hidden form tokens and Unicode CSV file numbers for all 47 prefectures (01..47)."""
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        page = client.get(NTA_ZENKEN_URL)
        page.raise_for_status()
    hiddens = dict(
        re.findall(
            r'<input[^>]+type=["\']hidden["\'][^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']*)["\']',
            page.text,
        )
    )
    blocks = re.findall(r'<dt class="mb05">([^<]+)</dt>\s*<dd>(.*?)</dd>', page.text, re.S)
    if len(blocks) < 97:
        raise RuntimeError(f"Unexpected NTA zenken page structure: found {len(blocks)} blocks")
    # blocks[49..97] is the Unicode CSV section: 49=全国, 50=北海道 .. 96=沖縄県, 97=国外
    unicode_prefs = blocks[50:97]
    if unicode_prefs[0][0].strip() != "北海道" or unicode_prefs[-1][0].strip() != "沖縄県":
        raise RuntimeError(
            f"Unexpected prefecture bounds: {unicode_prefs[0][0]} .. {unicode_prefs[-1][0]}"
        )
    entries: list[tuple[str, str, list[str]]] = []
    for idx, (pref_name, body) in enumerate(unicode_prefs, start=1):
        file_nos = re.findall(r"doDownload\((\d+)\)", body)
        entries.append((f"{idx:02d}", pref_name.strip(), file_nos))
    return hiddens, entries


def download_nta_file(
    client: httpx.Client, hiddens: dict[str, str], file_no: str, dest: Path
) -> Path:
    """POST to NTA zenken endpoint to download a single ZIP archive."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {**hiddens, "selDlFileNo": file_no}
    resp = client.post(NTA_POST_URL, data=payload)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


def build_national_full(root: Path, mhlw_limit: int | None = None, version: str = "v2") -> Path:
    """Build full All-Japan (47 prefectures) integrated DB package with low-disk/low-RAM streaming."""
    raw_dir = root / "data" / "raw_all"
    work_dir = root / "data" / "work_all"
    pkg_dir = root / "data" / "out" / f"jp_opendata_all_full_{version}"
    dist_dir = root / "dist"
    raw_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    hiddens, pref_entries = discover_nta_unicode_pref_files()
    print(f"Discovered {len(pref_entries)} prefectures from NTA Unicode CSV section.")

    # Step 1: Prefecture-by-prefecture NTA + ISJ geocoding -> intermediate Parquet (delete raw ZIPs immediately)
    with httpx.Client(timeout=180.0, follow_redirects=True) as client:
        for pref_code, pref_name, file_nos in pref_entries:
            corps_pq = work_dir / f"corps_{pref_code}.parquet"
            if corps_pq.exists():
                print(f"[{pref_code}/47 {pref_name}] Already built, skipping.")
                continue

            print(f"[{pref_code}/47 {pref_name}] Downloading {len(file_nos)} NTA archive(s)...")
            pref_dfs: list[pl.DataFrame] = []
            for part_idx, fno in enumerate(file_nos, start=1):
                nta_zip = raw_dir / f"nta_{pref_code}_p{part_idx}.zip"
                download_nta_file(client, hiddens, fno, nta_zip)
                pref_dfs.append(load_nta_corporates(nta_zip, limit=None))
                nta_zip.unlink(missing_ok=True)

            corps_df = pl.concat(pref_dfs).unique(subset=["corporate_number"], keep="first")
            del pref_dfs

            gaiku_zip = raw_dir / f"{pref_code}000-{MLIT_ISJ_GAIKU_VER}.zip"
            oaza_zip = raw_dir / f"{pref_code}000-{MLIT_ISJ_OAZA_VER}.zip"
            gaiku_url = f"https://nlftp.mlit.go.jp/isj/dls/data/{MLIT_ISJ_GAIKU_VER}/{pref_code}000-{MLIT_ISJ_GAIKU_VER}.zip"
            oaza_url = f"https://nlftp.mlit.go.jp/isj/dls/data/{MLIT_ISJ_OAZA_VER}/{pref_code}000-{MLIT_ISJ_OAZA_VER}.zip"
            download_file(gaiku_url, gaiku_zip)
            download_file(oaza_url, oaza_zip)

            corps_geo = geocode_corporates_with_isj(corps_df, gaiku_zip, oaza_zip)
            gaiku_zip.unlink(missing_ok=True)
            oaza_zip.unlink(missing_ok=True)
            del corps_df

            geo_cnt = corps_geo.filter(pl.col("latitude").is_not_null()).height
            print(
                f"[{pref_code}/47 {pref_name}] Corps: {corps_geo.height:,} | Geocoded: {geo_cnt:,} ({geo_cnt / max(corps_geo.height, 1):.1%})"
            )
            corps_geo.write_parquet(corps_pq, compression="zstd")
            del corps_geo
            gc.collect()

    # Step 2: National MLIT L01-26 land prices -> spatial enrichment per prefecture
    print("Downloading & parsing national MLIT L01-26 land prices...")
    l01_zip = download_file(MLIT_L01_ALL_URL, raw_dir / "L01-26_GML.zip")
    l01_points = parse_mlit_l01_gml(l01_zip).select(["price_yen_sqm", "latitude", "longitude"])
    l01_zip.unlink(missing_ok=True)
    print(f"Loaded {l01_points.height:,} national L01-26 land price points.")

    for pref_code, pref_name, _ in pref_entries:
        spatial_pq = work_dir / f"spatial_{pref_code}.parquet"
        if spatial_pq.exists():
            continue
        corps_geo = pl.read_parquet(
            work_dir / f"corps_{pref_code}.parquet",
            columns=["corporate_number", "latitude", "longitude"],
        )
        sp_df = enrich_corporates_spatial(corps_geo, land_price_points_df=l01_points)
        sp_df.write_parquet(spatial_pq, compression="zstd")
        del corps_geo, sp_df
    del l01_points
    gc.collect()

    # Step 3: National MHLW Care (ALL latest services) & FSA permits (ALL 8 categories across all sheets)
    kaigo_urls = discover_mhlw_kaigo_csvs(limit=mhlw_limit)
    print(f"Downloading & loading national MHLW Care Service CSVs ({len(kaigo_urls)} services)...")
    permit_records: list[PermitRecord] = []
    for url in kaigo_urls:
        fname = url.rsplit("/", 1)[-1]
        csv_path = download_file(url, raw_dir / fname)
        permit_records.extend(load_mhlw_kaigo_permits(csv_path, prefecture_filter=None))
        csv_path.unlink(missing_ok=True)

    print("Downloading & loading national FSA licensed-operator Excel lists (all 8 categories)...")
    for url, category in FSA_EXCEL_TARGETS:
        fname = "fsa_" + url.rsplit("/", 1)[-1]
        xlsx_path = download_file(url, raw_dir / fname)
        loaded = load_fsa_excel_permits(xlsx_path, category, prefecture_filter=None)
        print(f"  FSA [{category}]: {len(loaded):,} records")
        permit_records.extend(loaded)
        xlsx_path.unlink(missing_ok=True)

    deduped_permits = list({p.permit_id: p for p in permit_records}.values())
    del permit_records
    print(f"Resolving {len(deduped_permits):,} national permits against corporate index...")
    explicit_cnums = list({p.corporate_number for p in deduped_permits if p.corporate_number})
    norm_names = list(
        {normalize_company_name(p.raw_company_name, strip_legal_form=False) for p in deduped_permits}
    )
    core_names = list(
        {normalize_company_name(p.raw_company_name, strip_legal_form=True) for p in deduped_permits}
    )
    cand_corps_df = (
        pl.scan_parquet(str(work_dir / "corps_*.parquet"))
        .select(
            [
                "corporate_number",
                "norm_name",
                "core_name",
                "prefecture",
                "municipality",
                "street_key",
                "latitude",
                "longitude",
            ]
        )
        .filter(
            pl.col("corporate_number").is_in(explicit_cnums)
            | pl.col("norm_name").is_in(norm_names)
            | pl.col("core_name").is_in(core_names)
        )
        .collect()
    )
    permits_df = resolve_permits_to_corporates(deduped_permits, cand_corps_df)
    del deduped_permits, cand_corps_df
    gc.collect()

    # Step 4: EDINET full submitter bridge (official Edinetcode.zip + optional edinet-pit cohort metrics)
    edinet_zip = download_file(EDINET_CODELIST_URL, raw_dir / "Edinetcode.zip")
    edinet_parquet = root.parent / "edinet-pit-dataset" / "data" / "out" / "edinet_pit_cohort.parquet"
    all_corp_nums = set(
        pl.scan_parquet(str(work_dir / "corps_*.parquet"))
        .select("corporate_number")
        .collect()["corporate_number"]
        .to_list()
    )
    edinet_df = build_edinet_bridge_from_codelist(
        codelist_zip_path=edinet_zip,
        valid_corporate_numbers=all_corp_nums,
        edinet_parquet_path=edinet_parquet if edinet_parquet.exists() else None,
    )
    edinet_zip.unlink(missing_ok=True)
    del all_corp_nums
    gc.collect()

    sources_df = pl.DataFrame(
        [
            {
                "source_id": "nta_corp",
                "source_name": "国税庁法人番号公表サイト（全国47都道府県 Unicode CSV・閉鎖区分含む）を加工して作成",
                "publisher": "国税庁",
                "license_name": "公共データ利用規約（第1.0版 / PDL1.0）",
                "source_url": NTA_ZENKEN_URL,
                "retrieved_date": RETRIEVED_DATE,
            },
            {
                "source_id": "mlit_isj",
                "source_name": f"国土交通省 位置参照情報（全国47都道府県 街区{MLIT_ISJ_GAIKU_VER}・大字町丁目{MLIT_ISJ_OAZA_VER}）を加工して作成",
                "publisher": "国土交通省",
                "license_name": "位置参照情報利用約款（出典・加工表示の上で商用・有償二次配布可）",
                "source_url": "https://nlftp.mlit.go.jp/isj/",
                "retrieved_date": RETRIEVED_DATE,
            },
            {
                "source_id": "mlit_l01",
                "source_name": "国土数値情報 地価公示データ（全国 L01-26 / 令和8年版）を加工して作成",
                "publisher": "国土交通省",
                "license_name": "国土数値情報利用約款（L01商用可）",
                "source_url": MLIT_L01_ALL_URL,
                "retrieved_date": RETRIEVED_DATE,
            },
            {
                "source_id": "mhlw_kaigo",
                "source_name": f"介護サービス情報公表システム オープンデータ（全国・最新全{len(kaigo_urls)}サービス種別）を加工して作成",
                "publisher": "厚生労働省",
                "license_name": "CC BY 4.0 / 厚生労働省利用規約",
                "source_url": MHLW_OPENDATA_PAGE,
                "retrieved_date": RETRIEVED_DATE,
            },
            {
                "source_id": "fsa_menkyo",
                "source_name": "免許・許可・登録等を受けている業者一覧（金融庁Excel一括・全8業種）を加工して作成",
                "publisher": "金融庁",
                "license_name": "公共データ利用規約（第1.0版 / PDL1.0）",
                "source_url": "https://www.fsa.go.jp/menkyo/menkyo.html",
                "retrieved_date": RETRIEVED_DATE,
            },
            {
                "source_id": "fsa_edinet",
                "source_name": "金融庁 EDINET 提出者コード一覧（EdinetcodeDlInfo.csv）及び有価証券報告書データを加工して作成",
                "publisher": "金融庁",
                "license_name": "公共データ利用規約（第1.0版 / PDL1.0）",
                "source_url": "https://disclosure2.edinet-fsa.go.jp/",
                "retrieved_date": RETRIEVED_DATE,
            },
        ],
        schema=SOURCES_METADATA_SCHEMA,
    )

    # Step 5: Stream-merge intermediate Parquets into pkg_dir and delete work_dir/raw_dir
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    parquet_dir = pkg_dir / "jp_opendata_all_full_parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)

    print("Consolidating national Parquet tables...")
    pl.scan_parquet(str(work_dir / "corps_*.parquet")).unique(
        subset=["corporate_number"], keep="first"
    ).sink_parquet(parquet_dir / "corporates.parquet", compression="zstd")
    pl.scan_parquet(str(work_dir / "spatial_*.parquet")).unique(
        subset=["corporate_number"], keep="first"
    ).sort("corporate_number").sink_parquet(
        parquet_dir / "spatial_profiles.parquet", compression="zstd"
    )
    permits_df.write_parquet(parquet_dir / "permits.parquet", compression="zstd")
    edinet_df.write_parquet(parquet_dir / "edinet_links.parquet", compression="zstd")
    sources_df.write_parquet(parquet_dir / "_metadata_sources.parquet", compression="zstd")
    del permits_df, edinet_df, sources_df
    gc.collect()

    shutil.rmtree(work_dir, ignore_errors=True)
    shutil.rmtree(raw_dir, ignore_errors=True)

    # Step 6: Validate and export DuckDB + SQLite from Parquet
    print("Validating & exporting DuckDB + SQLite...")
    paths, report = export_from_parquet_dir(
        output_dir=pkg_dir,
        parquet_dir=parquet_dir,
        name="jp_opendata_all_full",
    )

    attribution_txt = "\n".join(
        [
            f"jp-opendata-db All-Japan Full Package ({version})",
            "加工主体: TerseLogic (https://terselogic.com/)",
            "本データベースは下記の公的オープンデータを正規化・名寄せ・空間結合して TerseLogic が独自に編集・加工したものです（各府省が作成した未加工データではありません）。",
            "",
            "一次出典および適用ライセンス:",
            f"- 「国税庁法人番号公表サイト 全件データ」（国税庁）({NTA_ZENKEN_URL}) を加工して作成 — 公共データ利用規約（第1.0版 / PDL1.0）",
            f"- 「位置参照情報（街区レベル {MLIT_ISJ_GAIKU_VER} / 大字・町丁目レベル {MLIT_ISJ_OAZA_VER}）」（国土交通省）(https://nlftp.mlit.go.jp/isj/) を加工して作成 — 位置参照情報利用約款（出典・加工表示の上で商用・有償二次配布可）",
            f"- 「国土数値情報 地価公示データ（L01-26 / 令和8年版）」（国土交通省）({MLIT_L01_ALL_URL}) を加工して作成 — 国土数値情報利用約款（L01商用可）",
            f"- 「介護サービス情報公表システム オープンデータ（最新全{len(kaigo_urls)}サービス種別）」（厚生労働省）({MHLW_OPENDATA_PAGE}) を加工して作成 — CC BY 4.0",
            "- 「免許・許可・登録等を受けている業者一覧（全8業種）」（金融庁）(https://www.fsa.go.jp/menkyo/menkyo.html) を加工して作成 — 公共データ利用規約（第1.0版 / PDL1.0 / https://www.fsa.go.jp/rules/index.html）",
            "- 「EDINET 提出者コード一覧・有価証券報告書データ」（金融庁）(https://disclosure2.edinet-fsa.go.jp/) を加工して作成 — 公共データ利用規約（第1.0版 / PDL1.0） / EDINET タクソノミ © Copyright 2014 Financial Services Agency, The Japanese Government.",
            "",
            report.to_markdown(),
        ]
    )
    attr_path = pkg_dir / "LICENSE_ATTRIBUTIONS.txt"
    attr_path.write_text(attribution_txt + "\n", encoding="utf-8")

    # Step 7: Streaming ZIP creation (delete each large artifact immediately after adding to ZIP to minimize peak disk usage)
    zip_path = dist_dir / f"jp_opendata_all_full_{version}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for file_path in [
            attr_path,
            paths["report"],
            paths["duckdb"],
            paths["sqlite"],
            *sorted(parquet_dir.glob("*.parquet")),
        ]:
            if file_path.exists():
                arcname = str(file_path.relative_to(pkg_dir))
                zf.write(file_path, arcname=arcname)
                file_path.unlink(missing_ok=True)
    shutil.rmtree(pkg_dir, ignore_errors=True)

    with zip_path.open("rb") as f:
        sha256 = hashlib.file_digest(f, "sha256").hexdigest()
    (dist_dir / f"jp_opendata_all_full_{version}.zip.sha256").write_text(
        f"{sha256}  {zip_path.name}\n", encoding="utf-8"
    )

    print(report.to_markdown())
    print(f"Distributable ZIP: {zip_path} ({zip_path.stat().st_size / (1024 * 1024):.1f} MB)")
    print(f"SHA-256: {sha256}")
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=["tokyo", "all"], default="tokyo")
    parser.add_argument("--mhlw-limit", type=int, default=None)
    parser.add_argument("--version", default="v2")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    if args.scope == "tokyo":
        build_tokyo_full(root, mhlw_limit=args.mhlw_limit or 35)
    elif args.scope == "all":
        build_national_full(root, mhlw_limit=args.mhlw_limit, version=args.version)


if __name__ == "__main__":
    main()
