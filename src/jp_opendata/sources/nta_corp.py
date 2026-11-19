"""Loader and normalizer for NTA Corporate Number (国税庁 法人番号) CSV/ZIP archives."""

from __future__ import annotations

import csv
import io
from pathlib import Path
import zipfile

import polars as pl

from jp_opendata.normalizer import (
    is_valid_corporate_number,
    normalize_address,
    normalize_company_name,
)

# 国税庁全件CSV（ヘッダ無し30列）の主要列インデックス
# 1: corporateNumber, 2: process (21=閉鎖等), 6: name, 9: prefectureName,
# 10: cityName, 11: streetNumber, 13: postCode, 18: closeDate
NTA_IDX_CORP_NUM = 1
NTA_IDX_PROCESS = 2
NTA_IDX_NAME = 6
NTA_IDX_PREF = 9
NTA_IDX_CITY = 10
NTA_IDX_STREET = 11
NTA_IDX_POSTCODE = 13
NTA_IDX_CLOSE_DATE = 18


CORPORATES_SCHEMA: dict[str, pl.DataType] = {
    "corporate_number": pl.String,
    "company_name": pl.String,
    "norm_name": pl.String,
    "core_name": pl.String,
    "process_code": pl.String,
    "is_closed": pl.Boolean,
    "close_date": pl.String,
    "prefecture": pl.String,
    "municipality": pl.String,
    "post_code": pl.String,
    "raw_address": pl.String,
    "norm_address": pl.String,
    "street_key": pl.String,
    "latitude": pl.Float64,
    "longitude": pl.Float64,
}


def _open_csv_text(path: Path) -> str:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise FileNotFoundError(f"No CSV found inside {path}")
            raw = zf.read(csv_names[0])
    else:
        raw = path.read_bytes()

    for enc in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def load_nta_corporates(
    path: Path,
    limit: int | None = None,
    validate_check_digit: bool = True,
) -> pl.DataFrame:
    """Load NTA corporate number CSV/ZIP into normalized corporates DataFrame."""
    text = _open_csv_text(path)
    reader = csv.reader(io.StringIO(text))
    rows: list[dict[str, object]] = []

    for idx, rec in enumerate(reader):
        if limit is not None and len(rows) >= limit:
            break
        # ヘッダ付きCSV（法人番号公表サイトの一部API/エクスポート）をスキップ
        if idx == 0 and rec and not rec[0].isdigit():
            continue
        if len(rec) <= NTA_IDX_POSTCODE:
            continue

        corp_num = rec[NTA_IDX_CORP_NUM].strip()
        if validate_check_digit and not is_valid_corporate_number(corp_num):
            continue

        name = rec[NTA_IDX_NAME].strip()
        if not name:
            continue

        process_code = rec[NTA_IDX_PROCESS].strip() if len(rec) > NTA_IDX_PROCESS else ""
        close_date = rec[NTA_IDX_CLOSE_DATE].strip() if len(rec) > NTA_IDX_CLOSE_DATE else ""
        is_closed = process_code == "21" or bool(close_date)

        pref = rec[NTA_IDX_PREF].strip()
        city = rec[NTA_IDX_CITY].strip()
        street = rec[NTA_IDX_STREET].strip()
        raw_addr = f"{pref}{city}{street}"
        addr = normalize_address(raw_addr)

        rows.append(
            {
                "corporate_number": corp_num,
                "company_name": name,
                "norm_name": normalize_company_name(name, strip_legal_form=False),
                "core_name": normalize_company_name(name, strip_legal_form=True),
                "process_code": process_code,
                "is_closed": is_closed,
                "close_date": close_date,
                "prefecture": addr.prefecture or pref,
                "municipality": addr.municipality or city,
                "post_code": rec[NTA_IDX_POSTCODE].strip(),
                "raw_address": raw_addr,
                "norm_address": addr.normalized,
                "street_key": addr.street_key,
                "latitude": None,
                "longitude": None,
            }
        )

    if not rows:
        return pl.DataFrame(schema=CORPORATES_SCHEMA)
    return pl.DataFrame(rows, schema=CORPORATES_SCHEMA)


def build_corporates_from_records(records: list[dict[str, object]]) -> pl.DataFrame:
    """Build normalized corporates DataFrame from structured dictionary records."""
    normalized_rows: list[dict[str, object]] = []
    for rec in records:
        corp_num = str(rec["corporate_number"]).strip()
        name = str(rec["company_name"]).strip()
        raw_addr = str(rec.get("raw_address") or "")
        addr = normalize_address(raw_addr)
        normalized_rows.append(
            {
                "corporate_number": corp_num,
                "company_name": name,
                "norm_name": normalize_company_name(name, strip_legal_form=False),
                "core_name": normalize_company_name(name, strip_legal_form=True),
                "process_code": str(rec.get("process_code") or "01"),
                "is_closed": bool(rec.get("is_closed", False)),
                "close_date": str(rec.get("close_date") or ""),
                "prefecture": str(rec.get("prefecture") or addr.prefecture),
                "municipality": str(rec.get("municipality") or addr.municipality),
                "post_code": str(rec.get("post_code") or ""),
                "raw_address": raw_addr,
                "norm_address": addr.normalized,
                "street_key": addr.street_key,
                "latitude": rec.get("latitude"),
                "longitude": rec.get("longitude"),
            }
        )
    if not normalized_rows:
        return pl.DataFrame(schema=CORPORATES_SCHEMA)
    return pl.DataFrame(normalized_rows, schema=CORPORATES_SCHEMA)
