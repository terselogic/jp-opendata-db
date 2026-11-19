"""Bridge connecting EDINET submitters & edinet-pit-dataset metrics to NTA Corporate Numbers."""

from __future__ import annotations

import csv
import io
from pathlib import Path
import zipfile

import polars as pl

from jp_opendata.normalizer import is_valid_corporate_number, normalize_company_name

EDINET_BRIDGE_SCHEMA: dict[str, pl.DataType] = {
    "corporate_number": pl.String,
    "edinet_code": pl.String,
    "sec_code": pl.String,
    "filer_name": pl.String,
    "listing_status": pl.String,
    "industry": pl.String,
    "latest_period_end": pl.Date,
    "net_sales": pl.Float64,
    "operating_income": pl.Float64,
    "net_assets": pl.Float64,
    "risk_change_score": pl.Float64,
}


def _load_edinet_metrics_summary(edinet_parquet_path: Path | None) -> pl.DataFrame:
    if edinet_parquet_path is None or not edinet_parquet_path.exists():
        return pl.DataFrame(
            schema={
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
    ed_df = pl.read_parquet(edinet_parquet_path)
    if ed_df.is_empty():
        return pl.DataFrame(
            schema={
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

    latest_doc = (
        ed_df.sort("submit_datetime")
        .group_by("edinet_code", maintain_order=True)
        .agg(
            pl.col("sec_code").last(),
            pl.col("filer_name").last(),
            pl.col("period_end").last().alias("latest_period_end"),
            pl.col("risk_change_score").drop_nulls().last().alias("risk_change_score"),
        )
    )

    pivoted = (
        ed_df.filter(pl.col("metric_name").is_in(["net_sales", "operating_income", "net_assets"]))
        .sort("submit_datetime")
        .group_by(["edinet_code", "metric_name"], maintain_order=True)
        .last()
        .pivot(on="metric_name", index="edinet_code", values="metric_value")
    )
    for col in ("net_sales", "operating_income", "net_assets"):
        if col not in pivoted.columns:
            pivoted = pivoted.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))

    return latest_doc.join(pivoted, on="edinet_code", how="left")


def build_edinet_bridge_from_codelist(
    codelist_zip_path: Path,
    valid_corporate_numbers: set[str],
    edinet_parquet_path: Path | None = None,
) -> pl.DataFrame:
    """Build full EDINET-to-NTA bridge from FSA official EdinetcodeDlInfo.csv + optional PIT metrics."""
    if not codelist_zip_path.exists() or not valid_corporate_numbers:
        return pl.DataFrame(schema=EDINET_BRIDGE_SCHEMA)

    with zipfile.ZipFile(codelist_zip_path) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            return pl.DataFrame(schema=EDINET_BRIDGE_SCHEMA)
        raw_text = zf.read(csv_names[0]).decode("cp932", errors="replace")

    reader = csv.reader(io.StringIO(raw_text))
    header: list[str] | None = None
    for row in reader:
        if row and any("ＥＤＩＮＥＴコード" in c or "EDINETコード" in c for c in row):
            header = [c.strip() for c in row]
            break
    if not header:
        return pl.DataFrame(schema=EDINET_BRIDGE_SCHEMA)

    col_idx = {name: i for i, name in enumerate(header)}
    ed_i = col_idx.get("ＥＤＩＮＥＴコード", col_idx.get("EDINETコード", 0))
    list_i = col_idx.get("上場区分", 2)
    name_i = col_idx.get("提出者名", 6)
    ind_i = col_idx.get("提出者業種", 10)
    sec_i = col_idx.get("証券コード", 11)
    corp_i = col_idx.get("提出者法人番号", 12)

    metrics_df = _load_edinet_metrics_summary(edinet_parquet_path)
    metrics_by_edinet: dict[str, dict[str, object]] = {
        str(r["edinet_code"]): r for r in metrics_df.to_dicts()
    }

    rows: list[dict[str, object]] = []
    seen_cnums: set[str] = set()
    for rec in reader:
        if len(rec) <= corp_i:
            continue
        ed_code = rec[ed_i].strip()
        cnum = rec[corp_i].strip()
        if not ed_code or not is_valid_corporate_number(cnum):
            continue
        if cnum not in valid_corporate_numbers or cnum in seen_cnums:
            continue
        seen_cnums.add(cnum)

        m = metrics_by_edinet.get(ed_code, {})
        sec_code = rec[sec_i].strip() if sec_i < len(rec) else ""
        rows.append(
            {
                "corporate_number": cnum,
                "edinet_code": ed_code,
                "sec_code": sec_code or str(m.get("sec_code") or "") or None,
                "filer_name": rec[name_i].strip(),
                "listing_status": rec[list_i].strip() if list_i < len(rec) else "",
                "industry": rec[ind_i].strip() if ind_i < len(rec) else "",
                "latest_period_end": m.get("latest_period_end"),
                "net_sales": m.get("net_sales"),
                "operating_income": m.get("operating_income"),
                "net_assets": m.get("net_assets"),
                "risk_change_score": m.get("risk_change_score"),
            }
        )

    if not rows:
        return pl.DataFrame(schema=EDINET_BRIDGE_SCHEMA)
    return pl.DataFrame(rows, schema=EDINET_BRIDGE_SCHEMA).sort("corporate_number")


def build_edinet_bridge(
    edinet_parquet_path: Path,
    corporates_df: pl.DataFrame,
) -> pl.DataFrame:
    """Link latest EDINET corporate metrics to NTA corporate_number via normalized name."""
    if not edinet_parquet_path.exists() or corporates_df.is_empty():
        return pl.DataFrame(schema=EDINET_BRIDGE_SCHEMA)

    summary = _load_edinet_metrics_summary(edinet_parquet_path)
    if summary.is_empty():
        return pl.DataFrame(schema=EDINET_BRIDGE_SCHEMA)

    corp_by_norm: dict[str, list[str]] = {}
    corp_by_core: dict[str, list[str]] = {}
    for row in corporates_df.select(["corporate_number", "norm_name", "core_name"]).to_dicts():
        cnum = str(row["corporate_number"])
        corp_by_norm.setdefault(str(row["norm_name"]), []).append(cnum)
        corp_by_core.setdefault(str(row["core_name"]), []).append(cnum)

    rows: list[dict[str, object]] = []
    for rec in summary.to_dicts():
        filer = str(rec["filer_name"] or "")
        norm = normalize_company_name(filer, strip_legal_form=False)
        core = normalize_company_name(filer, strip_legal_form=True)

        cands = corp_by_norm.get(norm, [])
        if len(cands) != 1:
            cands = corp_by_core.get(core, [])
        if len(cands) != 1:
            continue

        rows.append(
            {
                "corporate_number": cands[0],
                "edinet_code": rec["edinet_code"],
                "sec_code": rec["sec_code"],
                "filer_name": filer,
                "listing_status": "上場" if rec.get("sec_code") else "",
                "industry": "",
                "latest_period_end": rec["latest_period_end"],
                "net_sales": rec.get("net_sales"),
                "operating_income": rec.get("operating_income"),
                "net_assets": rec.get("net_assets"),
                "risk_change_score": rec.get("risk_change_score"),
            }
        )

    if not rows:
        return pl.DataFrame(schema=EDINET_BRIDGE_SCHEMA)
    return pl.DataFrame(rows, schema=EDINET_BRIDGE_SCHEMA)
