# jp-opendata-db

国税庁の法人番号（13桁チェックデジット検証済み）を主キーに、国土交通省「位置参照情報」、国土数値情報「地価公示（L01）」、厚生労働省「介護サービス事業所」、金融庁「免許・許可・登録等を受けている業者一覧」、および `edinet-pit-dataset` を正規化・名寄せ・空間結合した静的データベース（SQLite / DuckDB / Parquet）構築パイプライン。

## 収録データとライセンス（すべて商用二次配布可を確認済み）

1. **国税庁 法人番号公表サイト（東京都 分割1・2）**: 公共データ利用規約（第1.0版）
2. **国土交通省 位置参照情報（街区レベル `24.0a`・大字町丁目レベル `18.0b`）**: 位置参照情報利用約款（出典明記で有償二次配布可）
3. **国土交通省 国土数値情報 地価公示（`L01-25`）**: 国土数値情報利用約款（L01 商用可）
4. **厚生労働省 介護サービス情報公表システム オープンデータ**: CC BY 4.0 / 厚生労働省利用規約
5. **金融庁 免許・許可・登録等を受けている業者一覧（金商・貸金・資金移動）**: 政府標準利用規約（第2.0版）準拠

## 検証済み実測値（東京都 MVP）

- `corporates`: **1,373,848 社**（うち位置参照情報によるジオコーディング成功: **1,090,712 社 / 79.4%**）
- `permits`: **4,625 件**（うち法人番号名寄せ一致: **4,319 件 / 93.4%**）
- `spatial_profiles`（サンプル）: **2,000 社**（最寄り地価公示単価・距離付き）
- `edinet_links`: **79 社**
- `_metadata_sources`: **5 ソース**

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

## フル統合版（全国47都道府県・有償商用パッケージ）

本リポジトリの `sample/`（東京都 2,000社）でスキーマ・SQL結合をご確認のうえ、全国47都道府県を統合したビルド済みパッケージ（SQLite / DuckDB / Parquet / 出典表記一式）は以下の製品ページより入手できます。

- 製品ページ・購入案内: https://terselogic.com/datasets/jp-opendata/?utm_source=github&utm_medium=readme
- `corporates`: **5,830,372 社**（全国47都道府県）
- `spatial_profiles`: **3,555,962 社**（全国 L01-25 地価公示 25,563 地点と結合）
- `permits`: **97,581 件**（法人番号一致: **96,115 件 / 98.5%**）
- `edinet_links`: **95 社** / `_metadata_sources`: **5 機関**
- 配布アーカイブ: `jp_opendata_all_full_v1.zip`（1,531.8 MB）
