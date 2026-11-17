"""Unit tests for MHLW CSV and FSA Excel permit loaders."""

from __future__ import annotations

from pathlib import Path

import openpyxl

from jp_opendata.sources.permits import (
    load_fsa_excel_permits,
    load_mhlw_kaigo_permits,
)


def test_load_mhlw_kaigo_permits_filters_prefecture(tmp_path: Path) -> None:
    csv_path = tmp_path / "kaigo.csv"
    csv_path.write_text(
        "No,都道府県名,市区町村名,事業所名,住所,サービスの種類,法人番号,法人の名称,緯度,経度\n"
        "101,東京都,千代田区,ケアホーム丸の内,丸の内1-1-1,訪問介護,1000012010001,株式会社テスト介護,35.6812,139.7671\n"
        "102,大阪府,大阪市,大阪ケア,梅田1-1-1,訪問介護,2000012010002,株式会社大阪テスト,34.7025,135.4959\n",
        encoding="utf-8-sig",
    )

    records = load_mhlw_kaigo_permits(csv_path, prefecture_filter="東京都")
    assert len(records) == 1
    rec = records[0]
    assert rec.permit_id == "KAIGO-101"
    assert rec.permit_category == "訪問介護"
    assert rec.raw_company_name == "株式会社テスト介護"
    assert rec.raw_address == "東京都千代田区丸の内1-1-1"
    assert rec.corporate_number == "1000012010001"
    assert rec.latitude == 35.6812
    assert rec.longitude == 139.7671


def test_load_fsa_excel_permits_detects_varied_headers(tmp_path: Path) -> None:
    xlsx_path = tmp_path / "fsa_test.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "一覧"
    ws.append(["金融庁免許等一覧（テスト）"])
    ws.append(["管轄財務局", "登録番号", "商号又は名称", "法人番号", "本店等所在地"])
    ws.append(["関東財務局長", "第00001号", "株式会社東京フィナンシャル", "1000012010001", "東京都中央区日本橋1-1-1"])
    ws.append(["近畿財務局長", "第00002号", "株式会社関西フィナンシャル", "2000012010002", "大阪府大阪市中央区1-1-1"])
    wb.save(xlsx_path)

    records = load_fsa_excel_permits(
        xlsx_path,
        permit_category="テスト金融業者",
        prefecture_filter="東京都",
    )
    assert len(records) == 1
    rec = records[0]
    assert rec.permit_id == "FSA-第00001号"
    assert rec.authority_name == "金融庁"
    assert rec.raw_company_name == "株式会社東京フィナンシャル"
    assert rec.corporate_number == "1000012010001"
    assert rec.raw_address == "東京都中央区日本橋1-1-1"
