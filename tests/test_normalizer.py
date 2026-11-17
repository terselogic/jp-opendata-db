"""Unit tests for corporate number, company name, and address normalizers."""

from __future__ import annotations

from jp_opendata.normalizer import (
    compute_corporate_number_check_digit,
    is_valid_corporate_number,
    normalize_address,
    normalize_company_name,
)


def test_corporate_number_check_digit() -> None:
    # 国税庁: 7000012050002 / トヨタ自動車: 1180301018771
    assert compute_corporate_number_check_digit("000012050002") == "7"
    assert is_valid_corporate_number("7000012050002") is True
    assert is_valid_corporate_number("1180301018771") is True
    assert is_valid_corporate_number("9180301018771") is False
    assert is_valid_corporate_number("123") is False


def test_normalize_company_name_variants() -> None:
    a = normalize_company_name("㈱髙橋商事")
    b = normalize_company_name("（株）高橋商事")
    c = normalize_company_name("株式会社高橋商事")
    assert a == b == c == "株式会社高橋商事"

    assert normalize_company_name("﨑山建設（株）", strip_legal_form=True) == "崎山建設"
    assert normalize_company_name("ａｂｃテック株式会社", strip_legal_form=True) == "ABCテック"


def test_normalize_address_kanji_and_hyphens() -> None:
    res1 = normalize_address("東京都千代田区霞が関三丁目一番一号")
    res2 = normalize_address("東京都千代田区霞が関３－１－１　霞が関ビル5F")
    assert res1.prefecture == "東京都"
    assert res1.municipality == "千代田区"
    assert res1.normalized == "東京都千代田区霞が関3-1-1"
    assert res1.street_key == res2.street_key == "東京都千代田区霞が関3-1-1"
