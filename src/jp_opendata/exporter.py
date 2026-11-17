"""Exporter for SQLite, DuckDB, and Parquet/GeoParquet artifacts."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import sqlite3

import duckdb
import polars as pl

from jp_opendata.validator import QualityReport, validate_dataset


def _polars_to_sqlite_type(dtype: pl.DataType) -> str:
    if dtype in (pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64):
        return "INTEGER"
    if dtype in (pl.Float32, pl.Float64):
        return "REAL"
    return "TEXT"


def _write_sqlite_tables(sqlite_path: Path, tables: dict[str, pl.DataFrame]) -> None:
    if sqlite_path.exists():
        sqlite_path.unlink()
    con = sqlite3.connect(str(sqlite_path))
    try:
        for tbl_name, df in tables.items():
            cols_def = ", ".join(
                f'"{col}" {_polars_to_sqlite_type(dt)}'
                for col, dt in df.schema.items()
            )
            pk_clause = ""
            if tbl_name == "corporates":
                cols_def = cols_def.replace(
                    '"corporate_number" TEXT', '"corporate_number" TEXT PRIMARY KEY'
                )
            con.execute(f'CREATE TABLE "{tbl_name}" ({cols_def}{pk_clause})')
            if not df.is_empty():
                placeholders = ", ".join("?" for _ in df.columns)
                rows = []
                for row in df.rows():
                    cleaned = [
                        v.isoformat() if isinstance(v, (date, datetime)) else v
                        for v in row
                    ]
                    rows.append(tuple(cleaned))
                con.executemany(
                    f'INSERT INTO "{tbl_name}" VALUES ({placeholders})', rows
                )
        if "permits" in tables:
            con.execute(
                'CREATE INDEX IF NOT EXISTS idx_permits_corp ON "permits" ("corporate_number")'
            )
        if "spatial_profiles" in tables:
            con.execute(
                'CREATE INDEX IF NOT EXISTS idx_spatial_corp ON "spatial_profiles" ("corporate_number")'
            )
        if "edinet_links" in tables:
            con.execute(
                'CREATE INDEX IF NOT EXISTS idx_edinet_corp ON "edinet_links" ("corporate_number")'
            )
        con.commit()
    finally:
        con.close()


def export_integrated_db(
    output_dir: Path,
    corporates_df: pl.DataFrame,
    permits_df: pl.DataFrame,
    spatial_df: pl.DataFrame,
    edinet_df: pl.DataFrame,
    sources_df: pl.DataFrame,
    name: str = "jp_opendata",
) -> tuple[dict[str, Path], QualityReport]:
    """Validate all tables and export Parquet, DuckDB, and SQLite outputs."""
    report = validate_dataset(
        corporates_df=corporates_df,
        permits_df=permits_df,
        spatial_df=spatial_df,
        edinet_df=edinet_df,
        sources_df=sources_df,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    parquet_dir = output_dir / f"{name}_parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)

    tables: dict[str, pl.DataFrame] = {
        "corporates": corporates_df,
        "permits": permits_df,
        "spatial_profiles": spatial_df,
        "edinet_links": edinet_df,
        "_metadata_sources": sources_df,
    }

    paths: dict[str, Path] = {}
    duckdb_path = output_dir / f"{name}.duckdb"
    if duckdb_path.exists():
        duckdb_path.unlink()
    dcon = duckdb.connect(str(duckdb_path))
    try:
        for tbl_name, df in tables.items():
            pq_path = parquet_dir / f"{tbl_name}.parquet"
            df.write_parquet(pq_path, compression="zstd")
            paths[f"parquet_{tbl_name}"] = pq_path
            dcon.execute(
                f'CREATE OR REPLACE TABLE "{tbl_name}" AS SELECT * FROM read_parquet(?)',
                [str(pq_path)],
            )
    finally:
        dcon.close()
    paths["duckdb"] = duckdb_path

    sqlite_path = output_dir / f"{name}.sqlite"
    _write_sqlite_tables(sqlite_path, tables)
    paths["sqlite"] = sqlite_path

    report_path = output_dir / f"{name}_quality_report.md"
    report_path.write_text(report.to_markdown() + "\n", encoding="utf-8")
    paths["report"] = report_path

    return paths, report
