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


def test_normalize_address_preserves_katakana_choon_and_handles_no_connector() -> None:
    from jp_opendata.normalizer import strip_address_oaza_variants

    res_akita = normalize_address("秋田県南秋田郡井川町井内字杉ケ崎１番地の５")
    assert res_akita.street_key == "秋田県南秋田郡井川町井内字杉ケ崎1-5"
    variants = strip_address_oaza_variants(res_akita.street_key)
    assert "秋田県南秋田郡井川町井内" in variants

    res_bldg = normalize_address("秋田県北秋田市住吉町４番２３号鷹巣スポーツビル７号室")
    assert "スポーツビル" in res_bldg.normalized

    res_nagasaki = normalize_address("長崎県雲仙市国見町多比良乙２７３番地")
    n_variants = strip_address_oaza_variants(res_nagasaki.street_key)
    assert "長崎県雲仙市国見町多比良" in n_variants


def test_normalize_address_special_municipalities_and_sapporo_jo() -> None:
    assert normalize_address("三重県四日市市諏訪町1番5号").municipality == "四日市市"
    assert normalize_address("長崎県大村市玖島1丁目25番地").municipality == "大村市"
    assert normalize_address("佐賀県杵島郡大町町大字大町5017番地").municipality == "杵島郡大町町"
    assert normalize_address("広島県廿日市市下平良1丁目11番1号").municipality == "廿日市市"
    assert normalize_address("東京都町田市森野2丁目2番22号").municipality == "町田市"

    e = normalize_address("北海道札幌市中央区北四条東２丁目８番地６")
    w = normalize_address("北海道札幌市中央区北四条西１２丁目１番２８号")
    assert e.municipality == "札幌市中央区"
    assert w.municipality == "札幌市中央区"
    assert e.street_key == "北海道札幌市中央区北4条東2-8-6"
    assert w.street_key == "北海道札幌市中央区北4条西12-1-28"
    assert e.street_key != w.street_key

    assert normalize_address("東京都渋谷区道玄坂百二十三番地").street_key == "東京都渋谷区道玄坂123"
