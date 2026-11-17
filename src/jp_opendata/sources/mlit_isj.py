"""MLIT National Spatial Reference (位置参照情報) geocoder for corporate addresses."""

from __future__ import annotations

import csv
import io
from pathlib import Path
import re
import zipfile

import polars as pl

from jp_opendata.normalizer import normalize_address

# 街区キーから末尾の号・枝番（例: "-1-2" の最後の番号）を落として街区レベルに合わせる用
GAIIKU_PREFIX_RE = re.compile(r"^([^\d-]+(?:\d+(?:-\d+)?)?)")


def _extract_gaiku_lookup(zip_path: Path) -> dict[str, tuple[float, float]]:
    """Build street-block lookup: normalized(pref+muni+oaza+gaiku) -> (lat, lon)."""
    lookup: dict[str, tuple[float, float]] = {}
    with zipfile.ZipFile(zip_path) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            return lookup
        raw = zf.read(csv_names[0]).decode("cp932", errors="replace")

    reader = csv.DictReader(io.StringIO(raw))
    for row in reader:
        pref = (row.get("都道府県名") or "").strip()
        muni = (row.get("市区町村名") or "").strip()
        oaza = (row.get("大字・丁目名") or "").strip()
        koaza = (row.get("小字・通称名") or "").strip()
        gaiku = (row.get("街区符号・地番") or "").strip()
        lat_s = (row.get("緯度") or "").strip()
        lon_s = (row.get("経度") or "").strip()
        if not (pref and muni and oaza and lat_s and lon_s):
            continue
        try:
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            continue

        # 「千代田区永田町一丁目」+「6」 -> normalize_address -> "東京都千代田区永田町1-6"
        full = f"{pref}{muni}{oaza}{koaza}{gaiku}"
        norm = normalize_address(full).normalized
        if norm and norm not in lookup:
            lookup[norm] = (lat, lon)
    return lookup


def _extract_oaza_lookup(zip_path: Path) -> dict[str, tuple[float, float]]:
    """Build oaza/chome lookup: normalized(pref+muni+oaza) -> (lat, lon)."""
    lookup: dict[str, tuple[float, float]] = {}
    with zipfile.ZipFile(zip_path) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            return lookup
        raw = zf.read(csv_names[0]).decode("cp932", errors="replace")

    reader = csv.DictReader(io.StringIO(raw))
    for row in reader:
        pref = (row.get("都道府県名") or "").strip()
        muni = (row.get("市区町村名") or "").strip()
        oaza = (row.get("大字・丁目名") or "").strip()
        lat_s = (row.get("緯度") or "").strip()
        lon_s = (row.get("経度") or "").strip()
        if not (pref and muni and oaza and lat_s and lon_s):
            continue
        try:
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            continue
        norm = normalize_address(f"{pref}{muni}{oaza}").normalized
        if norm and norm not in lookup:
            lookup[norm] = (lat, lon)
    return lookup


def geocode_corporates_with_isj(
    corporates_df: pl.DataFrame,
    gaiku_zip_path: Path,
    oaza_zip_path: Path | None = None,
) -> pl.DataFrame:
    """Attach (latitude, longitude) to corporates using MLIT ISJ gaiku -> oaza fallback."""
    if corporates_df.is_empty():
        return corporates_df

    gaiku_map = _extract_gaiku_lookup(gaiku_zip_path)
    oaza_map = _extract_oaza_lookup(oaza_zip_path) if oaza_zip_path else {}

    lats: list[float | None] = []
    lons: list[float | None] = []
    for row in corporates_df.select(["street_key", "latitude", "longitude"]).to_dicts():
        if row["latitude"] is not None and row["longitude"] is not None:
            lats.append(row["latitude"])
            lons.append(row["longitude"])
            continue

        skey = str(row["street_key"] or "")
        coord: tuple[float, float] | None = gaiku_map.get(skey)
        if coord is None and "-" in skey:
            # 末尾の号を落として街区レベル（例: "...1-6-1" -> "...1-6"）で照合
            parts = skey.split("-")
            if len(parts) >= 3:
                coord = gaiku_map.get("-".join(parts[:-1]))
            if coord is None and oaza_map:
                # 町丁目レベル（例: "...1"）でフォールバック
                coord = oaza_map.get(parts[0])
        if coord is None and oaza_map:
            coord = oaza_map.get(skey)

        if coord is not None:
            lats.append(coord[0])
            lons.append(coord[1])
        else:
            lats.append(None)
            lons.append(None)

    return corporates_df.with_columns(
        pl.Series("latitude", lats, dtype=pl.Float64),
        pl.Series("longitude", lons, dtype=pl.Float64),
    )
