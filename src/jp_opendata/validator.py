"""Automated data quality, foreign-key, APPI PII, and license-attribution validator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import polars as pl

from jp_opendata.normalizer import is_valid_corporate_number

MOBILE_PHONE_RE = re.compile(r"\b0[789]0-\d{4}-\d{4}\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

SOURCES_METADATA_SCHEMA: dict[str, pl.DataType] = {
    "source_id": pl.String,
    "source_name": pl.String,
    "publisher": pl.String,
    "license_name": pl.String,
    "source_url": pl.String,
    "retrieved_date": pl.String,
}


@dataclass(frozen=True)
class QualityReport:
    """Objective dataset quality metrics for public reporting."""

    corporates_count: int
    permits_count: int
    permits_matched_count: int
    permits_match_rate: float
    spatial_enriched_count: int
    edinet_linked_count: int
    sources_count: int

    def to_markdown(self) -> str:
        """Render objective quality metrics as a concise Markdown summary."""
        return "\n".join(
            [
                "## Dataset Quality Report",
                "",
                f"- Corporates (`corporates`): {self.corporates_count:,}",
                f"- Permits (`permits`): {self.permits_count:,}",
                f"- Permits matched to Corporate Number: {self.permits_matched_count:,} ({self.permits_match_rate:.1%})",
                f"- Spatial enriched records (`spatial_profiles`): {self.spatial_enriched_count:,}",
                f"- EDINET linked corporations (`edinet_links`): {self.edinet_linked_count:,}",
                f"- Attribution sources (`_metadata_sources`): {self.sources_count}",
            ]
        )


def _scan_for_pii(df: pl.DataFrame, table_name: str) -> None:
    if df.is_empty():
        return
    string_cols = [c for c, dt in df.schema.items() if dt == pl.String]
    for col in string_cols:
        hits = df.filter(
            pl.col(col).is_not_null()
            & (
                pl.col(col).str.contains(MOBILE_PHONE_RE.pattern)
                | pl.col(col).str.contains(EMAIL_RE.pattern)
            )
        )
        if not hits.is_empty():
            raise ValueError(
                f"Potential PII (mobile phone / email) detected in {table_name}.{col}: {hits.height} rows"
            )


def _check_corporate_numbers_df(corp_nums_df: pl.DataFrame) -> None:
    """Validate 13-digit check digits and uniqueness on corporate_number column."""
    total = corp_nums_df.height
    if corp_nums_df["corporate_number"].n_unique() != total:
        raise ValueError("Duplicate corporate_number detected in corporates_df.")

    # Fast pre-filter for non-13-digit strings, then verify check digits in chunks
    bad_fmt = corp_nums_df.filter(
        pl.col("corporate_number").is_null()
        | ~pl.col("corporate_number").str.contains(r"^\d{13}$")
    )
    if not bad_fmt.is_empty():
        sample = bad_fmt["corporate_number"].head(5).to_list()
        raise ValueError(f"Invalid corporate_number values found: {sample}")

    for chunk in corp_nums_df.iter_slices(200_000):
        invalid = [c for c in chunk["corporate_number"].to_list() if not is_valid_corporate_number(c)]
        if invalid:
            raise ValueError(f"Invalid corporate_number values found: {invalid[:5]}")


def validate_dataset(
    corporates_df: pl.DataFrame,
    permits_df: pl.DataFrame,
    spatial_df: pl.DataFrame,
    edinet_df: pl.DataFrame,
    sources_df: pl.DataFrame,
) -> QualityReport:
    """Run all integrity, foreign-key, APPI PII, and license-attribution checks."""
    if corporates_df.is_empty():
        raise ValueError("corporates_df must not be empty.")
    if sources_df.is_empty():
        raise ValueError("_metadata_sources must contain attribution entries (PDL 1.0).")

    # 1. 法人番号チェックデジット・一意性検証
    corp_keys = corporates_df.select("corporate_number")
    _check_corporate_numbers_df(corp_keys)

    # 2. 外部キー整合性検証（Polars anti-join で省メモリ化）
    for tbl_name, tbl in (
        ("permits", permits_df),
        ("spatial_profiles", spatial_df),
        ("edinet_links", edinet_df),
    ):
        if tbl.is_empty():
            continue
        orphans = (
            tbl.select("corporate_number")
            .drop_nulls()
            .join(corp_keys, on="corporate_number", how="anti")
        )
        if not orphans.is_empty():
            sample = orphans["corporate_number"].head(5).to_list()
            raise ValueError(
                f"Foreign-key violation in {tbl_name}.corporate_number: {sample}"
            )

    # 3. APPI 個人情報混入防止チェック
    for tbl_name, tbl in (
        ("corporates", corporates_df),
        ("permits", permits_df),
        ("edinet_links", edinet_df),
    ):
        _scan_for_pii(tbl, tbl_name)

    permits_count = permits_df.height
    matched_count = (
        permits_df.filter(pl.col("corporate_number").is_not_null()).height
        if not permits_df.is_empty()
        else 0
    )
    match_rate = (matched_count / permits_count) if permits_count > 0 else 0.0

    return QualityReport(
        corporates_count=corporates_df.height,
        permits_count=permits_count,
        permits_matched_count=matched_count,
        permits_match_rate=match_rate,
        spatial_enriched_count=spatial_df.height,
        edinet_linked_count=edinet_df.height,
        sources_count=sources_df.height,
    )


def validate_parquet_dir(parquet_dir: Path) -> QualityReport:
    """Validate dataset directly from exported Parquet files with bounded memory."""
    corps_lf = pl.scan_parquet(parquet_dir / "corporates.parquet")
    permits_df = pl.read_parquet(parquet_dir / "permits.parquet")
    spatial_lf = pl.scan_parquet(parquet_dir / "spatial_profiles.parquet")
    edinet_df = pl.read_parquet(parquet_dir / "edinet_links.parquet")
    sources_df = pl.read_parquet(parquet_dir / "_metadata_sources.parquet")

    corp_keys = corps_lf.select("corporate_number").collect()
    if corp_keys.is_empty():
        raise ValueError("corporates must not be empty.")
    if sources_df.is_empty():
        raise ValueError("_metadata_sources must contain attribution entries (PDL 1.0).")

    _check_corporate_numbers_df(corp_keys)

    for tbl_name, lf in (
        ("permits", permits_df.lazy()),
        ("spatial_profiles", spatial_lf),
        ("edinet_links", edinet_df.lazy()),
    ):
        orphans = (
            lf.select("corporate_number")
            .drop_nulls()
            .join(corp_keys.lazy(), on="corporate_number", how="anti")
            .head(5)
            .collect()
        )
        if not orphans.is_empty():
            raise ValueError(
                f"Foreign-key violation in {tbl_name}.corporate_number: {orphans['corporate_number'].to_list()}"
            )

    for tbl_name, pq_name in (
        ("corporates", "corporates.parquet"),
        ("permits", "permits.parquet"),
        ("edinet_links", "edinet_links.parquet"),
    ):
        lf = pl.scan_parquet(parquet_dir / pq_name)
        str_cols = [c for c, dt in lf.collect_schema().items() if dt == pl.String]
        for col in str_cols:
            hits = (
                lf.filter(
                    pl.col(col).is_not_null()
                    & (
                        pl.col(col).str.contains(MOBILE_PHONE_RE.pattern)
                        | pl.col(col).str.contains(EMAIL_RE.pattern)
                    )
                )
                .select(pl.len())
                .collect()
            )
            cnt = int(hits.item())
            if cnt > 0:
                raise ValueError(
                    f"Potential PII (mobile phone / email) detected in {tbl_name}.{col}: {cnt} rows"
                )

    spatial_count = int(spatial_lf.select(pl.len()).collect().item())
    permits_count = permits_df.height
    matched_count = (
        permits_df.filter(pl.col("corporate_number").is_not_null()).height
        if not permits_df.is_empty()
        else 0
    )
    match_rate = (matched_count / permits_count) if permits_count > 0 else 0.0

    return QualityReport(
        corporates_count=corp_keys.height,
        permits_count=permits_count,
        permits_matched_count=matched_count,
        permits_match_rate=match_rate,
        spatial_enriched_count=spatial_count,
        edinet_linked_count=edinet_df.height,
        sources_count=sources_df.height,
    )
