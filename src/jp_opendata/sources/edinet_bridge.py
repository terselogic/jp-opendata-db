"""Bridge connecting edinet-pit-dataset company disclosures to NTA Corporate Numbers."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from jp_opendata.normalizer import normalize_company_name

EDINET_BRIDGE_SCHEMA: dict[str, pl.DataType] = {
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


def build_edinet_bridge(
    edinet_parquet_path: Path,
    corporates_df: pl.DataFrame,
) -> pl.DataFrame:
    """Link latest EDINET corporate metrics to NTA corporate_number via normalized name."""
    if not edinet_parquet_path.exists() or corporates_df.is_empty():
        return pl.DataFrame(schema=EDINET_BRIDGE_SCHEMA)

    ed_df = pl.read_parquet(edinet_parquet_path)
    if ed_df.is_empty():
        return pl.DataFrame(schema=EDINET_BRIDGE_SCHEMA)

    # 企業ごとに最新提出日時のレコードへ集約し、主要財務3指標をピボット
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

    summary = latest_doc.join(pivoted, on="edinet_code", how="left")

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
