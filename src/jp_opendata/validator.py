"""Automated data quality, foreign-key, APPI PII, and license-attribution validator."""

from __future__ import annotations

from dataclasses import dataclass
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
    corp_nums = corporates_df["corporate_number"].to_list()
    invalid_cnums = [c for c in corp_nums if not isinstance(c, str) or not is_valid_corporate_number(c)]
    if invalid_cnums:
        raise ValueError(f"Invalid corporate_number values found: {invalid_cnums[:5]}")
    if corporates_df["corporate_number"].n_unique() != corporates_df.height:
        raise ValueError("Duplicate corporate_number detected in corporates_df.")

    known_set = set(corp_nums)

    # 2. 外部キー整合性検証
    for tbl_name, tbl in (
        ("permits", permits_df),
        ("spatial_profiles", spatial_df),
        ("edinet_links", edinet_df),
    ):
        if tbl.is_empty():
            continue
        non_null_cnums = [
            c for c in tbl["corporate_number"].drop_nulls().to_list() if c not in known_set
        ]
        if non_null_cnums:
            raise ValueError(
                f"Foreign-key violation in {tbl_name}.corporate_number: {non_null_cnums[:5]}"
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
