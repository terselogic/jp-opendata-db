"""Permit registry loader and multi-stage corporate entity resolution."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import io
from pathlib import Path
import re

import openpyxl
import polars as pl
import pypdfium2 as pdfium

from jp_opendata.normalizer import (
    is_valid_corporate_number,
    normalize_address,
    normalize_company_name,
)

PERMITS_SCHEMA: dict[str, pl.DataType] = {
    "permit_id": pl.String,
    "permit_category": pl.String,
    "authority_name": pl.String,
    "raw_company_name": pl.String,
    "raw_address": pl.String,
    "corporate_number": pl.String,
    "match_tier": pl.String,
    "match_confidence": pl.Float64,
    "latitude": pl.Float64,
    "longitude": pl.Float64,
}


@dataclass(frozen=True)
class PermitRecord:
    """Raw permit entry prior to entity resolution."""

    permit_id: str
    permit_category: str
    authority_name: str
    raw_company_name: str
    raw_address: str
    corporate_number: str | None = None
    latitude: float | None = None
    longitude: float | None = None


def extract_pdf_text_lines(pdf_path: Path, max_pages: int | None = None) -> list[str]:
    """Extract raw text lines from a permit registry PDF using pypdfium2."""
    doc = pdfium.PdfDocument(str(pdf_path))
    lines: list[str] = []
    try:
        total = len(doc) if max_pages is None else min(len(doc), max_pages)
        for i in range(total):
            page = doc[i]
            textpage = page.get_textpage()
            text = textpage.get_text_range()
            for raw_line in text.splitlines():
                cleaned = re.sub(r"\s+", " ", raw_line).strip()
                if cleaned:
                    lines.append(cleaned)
    finally:
        doc.close()
    return lines


def resolve_permits_to_corporates(
    permits: list[PermitRecord],
    corporates_df: pl.DataFrame,
) -> pl.DataFrame:
    """Resolve permit records against NTA corporates using deterministic multi-tier matching."""
    if not permits:
        return pl.DataFrame(schema=PERMITS_SCHEMA)

    # Pre-filter corporates_df to only candidate rows relevant to permits (bounded memory for 5M+ rows)
    explicit_cnums = {p.corporate_number for p in permits if p.corporate_number}
    norm_names = {normalize_company_name(p.raw_company_name, strip_legal_form=False) for p in permits}
    core_names = {normalize_company_name(p.raw_company_name, strip_legal_form=True) for p in permits}
    filtered_corps = corporates_df.filter(
        pl.col("corporate_number").is_in(list(explicit_cnums))
        | pl.col("norm_name").is_in(list(norm_names))
        | pl.col("core_name").is_in(list(core_names))
    )

    select_cols = ["corporate_number", "norm_name", "core_name", "prefecture", "municipality", "street_key"]
    has_coords = "latitude" in filtered_corps.columns and "longitude" in filtered_corps.columns
    if has_coords:
        select_cols.extend(["latitude", "longitude"])
    corp_rows = filtered_corps.select(select_cols).to_dicts()
    known_corp_nums = {str(r["corporate_number"]) for r in corp_rows}
    corp_coords: dict[str, tuple[float | None, float | None]] = {
        str(r["corporate_number"]): (r.get("latitude"), r.get("longitude"))
        for r in corp_rows
        if has_coords and r.get("latitude") is not None and r.get("longitude") is not None
    }

    # Tier 2: (norm_name, municipality, street_key) -> [corporate_number]
    tier2_index: dict[tuple[str, str, str], list[str]] = {}
    # Tier 3: (core_name, municipality) -> [corporate_number]
    tier3_index: dict[tuple[str, str], list[str]] = {}
    # Tier 4: (core_name, prefecture) -> [corporate_number]
    tier4_index: dict[tuple[str, str], list[str]] = {}

    for r in corp_rows:
        cnum = str(r["corporate_number"])
        tier2_index.setdefault(
            (str(r["norm_name"]), str(r["municipality"]), str(r["street_key"])), []
        ).append(cnum)
        tier3_index.setdefault(
            (str(r["core_name"]), str(r["municipality"])), []
        ).append(cnum)
        tier4_index.setdefault(
            (str(r["core_name"]), str(r["prefecture"])), []
        ).append(cnum)

    resolved_rows: list[dict[str, object]] = []
    for p in permits:
        norm_name = normalize_company_name(p.raw_company_name, strip_legal_form=False)
        core_name = normalize_company_name(p.raw_company_name, strip_legal_form=True)
        addr = normalize_address(p.raw_address)

        matched_cnum: str | None = None
        tier = "unmatched"
        confidence = 0.0

        if (
            p.corporate_number
            and is_valid_corporate_number(p.corporate_number)
            and p.corporate_number in known_corp_nums
        ):
            matched_cnum = p.corporate_number
            tier = "tier1_explicit_corp_num"
            confidence = 1.0
        else:
            cands2 = tier2_index.get((norm_name, addr.municipality, addr.street_key), [])
            if len(cands2) == 1:
                matched_cnum = cands2[0]
                tier = "tier2_exact_name_street"
                confidence = 0.95
            else:
                cands3 = tier3_index.get((core_name, addr.municipality), [])
                if len(cands3) == 1:
                    matched_cnum = cands3[0]
                    tier = "tier3_core_name_municipality"
                    confidence = 0.85
                else:
                    cands4 = tier4_index.get((core_name, addr.prefecture), [])
                    if len(cands4) == 1:
                        matched_cnum = cands4[0]
                        tier = "tier4_core_name_prefecture"
                        confidence = 0.70
                    elif len(cands2) > 1 or len(cands3) > 1 or len(cands4) > 1:
                        tier = "ambiguous"

        lat = p.latitude
        lon = p.longitude
        if (lat is None or lon is None) and matched_cnum and matched_cnum in corp_coords:
            lat, lon = corp_coords[matched_cnum]

        resolved_rows.append(
            {
                "permit_id": p.permit_id,
                "permit_category": p.permit_category,
                "authority_name": p.authority_name,
                "raw_company_name": p.raw_company_name,
                "raw_address": p.raw_address,
                "corporate_number": matched_cnum,
                "match_tier": tier,
                "match_confidence": confidence,
                "latitude": lat,
                "longitude": lon,
            }
        )

    return pl.DataFrame(resolved_rows, schema=PERMITS_SCHEMA)


def load_mhlw_kaigo_permits(
    csv_path: Path,
    prefecture_filter: str | None = "東京都",
) -> list[PermitRecord]:
    """Load MHLW Care Service Open Data CSV (CC BY) into PermitRecord list."""
    raw = csv_path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    records: list[PermitRecord] = []

    for row in reader:
        pref = (row.get("都道府県名") or "").strip()
        if prefecture_filter and pref != prefecture_filter:
            continue
        corp_name = (row.get("法人の名称") or row.get("事業所名") or "").strip()
        if not corp_name:
            continue
        muni = (row.get("市区町村名") or "").strip()
        addr_body = (row.get("住所") or "").strip()
        full_addr = addr_body if addr_body.startswith(pref) else f"{pref}{addr_body}"
        if pref and muni and muni not in full_addr:
            full_addr = f"{pref}{muni}{addr_body}"

        lat_s = (row.get("緯度") or "").strip()
        lon_s = (row.get("経度") or "").strip()
        try:
            lat = float(lat_s) if lat_s else None
            lon = float(lon_s) if lon_s else None
        except ValueError:
            lat, lon = None, None

        cnum = (row.get("法人番号") or "").strip() or None
        pid = (row.get("No") or "").strip()
        service = (row.get("サービスの種類") or "介護サービス").strip()
        if not pid:
            continue

        code_match = re.search(r"jigyosho_(\d+)_", csv_path.name)
        service_key = code_match.group(1) if code_match else service

        records.append(
            PermitRecord(
                permit_id=f"KAIGO-{service_key}-{pid}",
                permit_category=service,
                authority_name="厚生労働省・都道府県",
                raw_company_name=corp_name,
                raw_address=full_addr,
                corporate_number=cnum,
                latitude=lat,
                longitude=lon,
            )
        )
    return records


FSA_CATEGORY_SLUGS: dict[str, str] = {
    "金融商品取引業者": "kinyushohin",
    "貸金業者（財務局長登録）": "kasi",
    "資金移動業者": "shikin_idou",
    "暗号資産交換業者": "kasoutuka",
    "銀行": "ginkou",
    "信用金庫": "shinkin",
    "前払式支払手段（第三者型）": "daisan",
    "監査法人": "kansahoujin",
}


def load_fsa_excel_permits(
    xlsx_path: Path,
    permit_category: str,
    prefecture_filter: str | None = "東京都",
) -> list[PermitRecord]:
    """Load FSA licensed-operator Excel list (.xlsx) across ALL sheets into PermitRecord list."""
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_path.read_bytes()), read_only=True, data_only=True)
    cat_slug = FSA_CATEGORY_SLUGS.get(permit_category, re.sub(r"\s+", "_", permit_category))
    records: list[PermitRecord] = []

    try:
        for sheet_idx, sheet_name in enumerate(wb.sheetnames):
            ws = wb[sheet_name]
            rows_iter = ws.iter_rows(values_only=True)

            col_idx: dict[str, int] = {}
            for row in rows_iter:
                cells = [str(c).replace("\n", "").strip() if c is not None else "" for c in row]
                has_reg = any(k in cells for k in ("登録番号", "免許番号", "許可番号"))
                has_corp = "法人番号" in cells
                has_loc = any(
                    k in cells
                    for k in ("本店等所在地", "本店所在地", "事務所の所在地", "所在地")
                )
                has_name = any(
                    c.endswith("名") or "商号" in c or "名称" in c
                    for c in cells
                    if c and c != "都道府県名"
                )
                if (has_reg or has_corp) and (has_loc or has_name):
                    for idx, label in enumerate(cells):
                        if label and label not in col_idx:
                            col_idx[label] = idx
                    break

            if not col_idx:
                continue

            reg_i = next(
                (col_idx[k] for k in ("登録番号", "免許番号", "許可番号") if k in col_idx),
                None,
            )
            corp_i = col_idx.get("法人番号")
            addr_i = next(
                (
                    col_idx[k]
                    for k in ("本店等所在地", "本店所在地", "事務所の所在地", "所在地")
                    if k in col_idx
                ),
                None,
            )
            name_i = next(
                (
                    idx
                    for label, idx in col_idx.items()
                    if label != "都道府県名"
                    and (label.endswith("名") or "商号" in label or "名称" in label)
                ),
                None,
            )
            if name_i is None:
                continue

            seq = 0
            for row in rows_iter:
                if len(row) <= name_i:
                    continue
                raw_name = str(row[name_i] or "").strip()
                if not raw_name or raw_name in ("銀行名", "名称", "商号又は名称"):
                    continue

                reg_no = ""
                if reg_i is not None and reg_i < len(row):
                    reg_no = str(row[reg_i] or "").replace("\n", " ").strip()

                raw_cnum = row[corp_i] if corp_i is not None and corp_i < len(row) else None
                cnum_str = str(raw_cnum).strip() if raw_cnum is not None else ""
                if cnum_str.endswith(".0"):
                    cnum_str = cnum_str[:-2]
                cnum = cnum_str if cnum_str.isdigit() and len(cnum_str) == 13 else None

                # 登録番号列があるシートでは登録番号欠損行（注記・集計行等）をスキップ
                if reg_i is not None and not reg_no:
                    continue
                # 登録番号列がないシート（銀行・信用金庫等）では法人番号・住所いずれも空の注記行をスキップ
                addr = str(row[addr_i] or "").strip() if addr_i is not None and addr_i < len(row) else ""
                if reg_i is None and not cnum and not addr:
                    continue

                # 脚注記号（例: "※1"）を除去
                cleaned_name = re.sub(r"※\d+$", "", raw_name).strip()
                if prefecture_filter and not addr.startswith(prefecture_filter):
                    continue

                seq += 1
                id_part = reg_no or cnum or f"s{sheet_idx}-{seq}"
                records.append(
                    PermitRecord(
                        permit_id=f"FSA-{cat_slug}-{id_part}",
                        permit_category=permit_category,
                        authority_name="金融庁",
                        raw_company_name=cleaned_name,
                        raw_address=addr,
                        corporate_number=cnum,
                    )
                )
    finally:
        wb.close()
    return records
