# jp-opendata-db

> **English Summary:** Integrated Japanese public open data pipeline and prebuilt SQLite/DuckDB/Parquet dataset joining 5.83M National Tax Agency corporate numbers (with `is_closed` / `process_code` / `close_date`) with MLIT geocoded land prices (`L01-26`, **5.72M / 98.1% matched**), MHLW care services (all 35 categories) & FSA financial permits (all 8 categories: **226,188 records, 98.8% matched**), and full EDINET submitter links (**7,284**), strictly from commercially redistributable public licenses.
> - **English Datasets Hub:** https://terselogic.com/datasets/?utm_source=github&utm_medium=readme
> - **Full Archive & Licensing (JA):** https://terselogic.com/datasets/jp-opendata/?utm_source=github&utm_medium=readme
> - **Free Tokyo Sample on Hugging Face:** https://huggingface.co/datasets/terselogic/jp-opendata-sample

国税庁の法人番号（13桁チェックデジット検証済み・閉鎖法人区分付き）を主キーに、国土交通省「位置参照情報（街区 24.0a・大字町丁目 19.0b）」、国土数値情報「地価公示（L01-26）」、厚生労働省「介護サービス事業所（全35種別）」、金融庁「免許・許可・登録等を受けている業者一覧（全8業種）」、および金融庁「EDINET提出者コード一覧（`EdinetcodeDlInfo.csv`）」を正規化・名寄せ・空間結合した静的データベース（SQLite / DuckDB / Parquet）構築パイプライン。

## 収録データとライセンス（すべて商用二次配布可を確認済み）

1. **国税庁 法人番号公表サイト（全国47都道府県 全件・閉鎖区分含む）**: 公共データ利用規約（第1.0版 / PDL 1.0）
2. **国土交通省 位置参照情報（街区レベル `24.0a`・大字町丁目レベル `19.0b`）**: 位置参照情報利用約款（出典・加工表示の上で商用・有償二次配布可）
3. **国土交通省 国土数値情報 地価公示（`L01-26` / 令和8年版）**: 国土数値情報利用約款（L01 商用可）
4. **厚生労働省 介護サービス情報公表システム オープンデータ（最新全35サービス種別）**: CC BY 4.0 / 厚生労働省利用規約
5. **金融庁 免許・許可・登録等を受けている業者一覧（全8業種：金商・貸金・資金移動・暗号資産・銀行・信用金庫・前払式・監査法人）**: 公共データ利用規約（第1.0版 / PDL 1.0）
6. **金融庁 EDINET 提出者コード一覧・有価証券報告書データ**: 公共データ利用規約（第1.0版 / PDL 1.0） / EDINET タクソノミ

## 検証済み実測値（無料サンプル：`sample/` 東京都 2,000 社）

- `corporates`: **2,000 社**（東京都・ジオコーディング・閉鎖区分カラム付き）
- `permits`: **4,448 件**（法人番号一致: **4,448 件 / 100.0%**、厚労省＋金融庁6業種）
- `spatial_profiles`: **2,000 社**（最寄り地価公示単価 `L01-26`・距離付き）
- `edinet_links`: **2,000 社**
- `_metadata_sources`: **6 ソース**（`sample/LICENSE_ATTRIBUTIONS.txt` 同梱）

## クイックスタート

```bash
uv sync --all-extras
uv run pytest -v
uv run python scripts/build_mvp.py
```

## サンプル SQL（SQLite / DuckDB）

```sql
SELECT c.corporate_number,
       c.company_name,
       p.permit_category,
       p.match_tier,
       s.nearest_land_price_yen_sqm,
       s.nearest_land_price_dist_m
FROM corporates c
JOIN permits p ON c.corporate_number = p.corporate_number
JOIN spatial_profiles s ON c.corporate_number = s.corporate_number
LIMIT 10;
```

## フル統合版（全国47都道府県・有償商用パッケージ v2）

本リポジトリの `sample/`（東京都 2,000社）でスキーマ・SQL結合をご確認のうえ、全国47都道府県を統合したビルド済みパッケージ（SQLite / DuckDB / Parquet / 出典表記一式）は以下の製品ページより入手できます。

- 製品ページ・購入案内: https://terselogic.com/datasets/jp-opendata/?utm_source=github&utm_medium=readme
- `corporates`: **5,830,372 社**（全国47都道府県・現役 `5,039,736` 社 / 閉鎖 `790,636` 社を `is_closed` で識別可能）
- `spatial_profiles`: **5,722,211 社 / 98.1%**（全国 `L01-26` 地価公示 25,565 地点と結合）
- `permits`: **226,188 件**（厚労省最新全35種＋金融庁全8業種 / 法人番号一致: **223,414 件 / 98.8%** / ジオコーディング率 **99.9%**）
- `edinet_links`: **7,284 社** / `_metadata_sources`: **6 ソース**
- 配布アーカイブ: `jp_opendata_all_full_v2.zip`（1,750.2 MB / SHA-256: `8c033948fa0a832e163b8ba845858208f99d2a6c807a2a94e2f6bc2980ba040f`）
