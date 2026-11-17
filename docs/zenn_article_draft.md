---
title: "国税庁法人番号×位置参照情報×許認可台帳の名寄せ統合DB設計（137万社検証）"
emoji: "🗂️"
type: "tech"
topics: ["python", "duckdb", "sqlite", "opendata", "gis"]
published: true
---

日本の公的オープンデータ（国税庁の法人番号、国土交通省の位置参照情報・地価公示、厚生労働省・金融庁の許認可/事業所台帳、EDINET企業開示）を、商用利用・二次配布可能なライセンス条件のみに絞って統合するパイプライン `jp-opendata-db` を構築し、東京都データで実測検証しました。

- GitHub（パイプライン＋2,000社サンプルDB）: https://github.com/terselogic/jp-opendata-db
- Hugging Face Datasets: https://huggingface.co/datasets/terselogic/jp-opendata-sample

## 1. 商用二次配布できるデータソースの選定

事前調査で一次規約を確認した結果、国土数値情報の「用途地域（A29）」や「洪水浸水想定区域（A31）」など一部データは詳細ページ・FAQ上で「非商用」制限（販売製品・ソフト同梱不可）が明記されています。また、国交省の宅建業者・建設業者検索システムは公式の一括CSV配布がありません。

そのため、本設計では商用二次配布が可能な以下5ソースのみで構成しています。

| データソース | 主な用途 | 適用規約・条件 |
| :--- | :--- | :--- |
| 国税庁 法人番号公表サイト（東京都 分割1・2） | 法人マスタ（13桁チェックデジット検証） | 公共データ利用規約（第1.0版） |
| 国交省 位置参照情報（街区 `24.0a`・町丁目 `18.0b`） | 住所→緯度経度ジオコーディング | 位置参照情報利用約款（Q20/Q21で有償二次配布可） |
| 国交省 国土数値情報 地価公示（`L01-25`） | 最寄り公示地価・距離計算 | 国土数値情報利用約款（L01商用可） |
| 厚労省 介護サービス情報公表システム（訪問介護） | 事業所・法人番号欠損レコードの名寄せ | CC BY 4.0 / 厚労省利用規約 |
| 金融庁 免許・許可・登録業者一覧（金商・貸金・資金移動） | 登録業者Excel（脚注ノイズ除去・名寄せ） | 政府標準利用規約（第2.0版）準拠 |

## 2. 実装した正規化と多段名寄せ

公的台帳を結合する際の主な障壁は、文字コード混在（UTF-8 / Shift-JIS）、国税庁データの分割ZIP（東京都は分割1・2に分かれている）、法人格・旧字体の揺れ（`㈱`/`（株）`/`髙`/`﨑`）、漢数字住所（`三丁目一番一号`）、および許認可台帳側の法人番号欠損や脚注記号（`株式会社○○※1`）です。

`src/jp_opendata/normalizer.py` と `permits.py` では以下を実装しました。

1. **13桁法人番号チェックデジット検証**: 国税庁公式の検査数字計算で不正レコードを除外
2. **商号・住所正規化**: NFKC変換、旧字体マッピング、法人格統一、漢数字→アラビア数字の番地キー抽出
3. **4段階の決定論的名寄せ**:
   - `tier1_explicit_corp_num` (1.00): 元データ記載の有効な法人番号
   - `tier2_exact_name_street` (0.95): 正規化商号＋市区町村＋番地キー一致
   - `tier3_core_name_municipality` (0.85): コア社名＋市区町村の一意一致
   - `tier4_core_name_prefecture` (0.70): コア社名＋都道府県の一意一致（複数候補時は誤結合を防ぐため `ambiguous` として保留）

## 3. 東京都データでの実測結果

`uv run python scripts/build_mvp.py` による実測値は以下の通りです。

- **法人マスタ (`corporates`)**: 1,373,848 社
- **位置参照情報ジオコーディング成功**: 1,090,712 社（79.4%）
- **許認可・事業所 (`permits`)**: 4,625 件
- **法人番号への名寄せ一致**: 4,319 件（93.4%）
- **EDINET財務リンク (`edinet_links`)**: 79 社（サンプルSQLite内には71社を収録）
- **空間結合 (`spatial_profiles`)**: 2,000 社（DuckDB Spatialによる最寄り地価公示単価・距離計算）

## 4. SQLite / DuckDB での結合クエリ例

公開しているサンプルDB（`sample/jp_opendata_sample.sqlite`）では、上場企業財務と最寄り地価公示・許認可を1本のSQLで結合できます。

```sql
SELECT c.corporate_number,
       c.company_name,
       e.sec_code,
       e.net_sales,
       s.nearest_land_price_yen_sqm,
       s.nearest_land_price_dist_m
FROM corporates c
JOIN edinet_links e ON c.corporate_number = e.corporate_number
JOIN spatial_profiles s ON c.corporate_number = s.corporate_number
ORDER BY e.net_sales DESC NULLS LAST
LIMIT 5;
```

実測出力例（`ANAホールディングス`、`大東建託`、`長谷工コーポレーション` 等）や検証コードは GitHub リポジトリの `tests/` および `scripts/build_mvp.py` を参照してください。
