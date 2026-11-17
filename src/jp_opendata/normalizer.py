"""Japanese corporate name, address, and corporate-number normalization utilities."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

# 旧字体・異体字 -> 常用漢字への代表マッピング（名寄せ用）
VARIANT_KANJI_MAP: dict[str, str] = str.maketrans(
    {
        "髙": "高",
        "﨑": "崎",
        "澤": "沢",
        "邊": "辺",
        "邉": "辺",
        "齊": "斉",
        "齋": "斉",
        "國": "国",
        "廣": "広",
        "櫻": "桜",
        "濱": "浜",
        "實": "実",
        "學": "学",
        "榮": "栄",
        "萬": "万",
        "鐵": "鉄",
        "會": "会",
        "體": "体",
        "當": "当",
        "龍": "竜",
        "德": "徳",
        "惠": "恵",
    }
)

# 法人格の表記揺れ（コア社名抽出および前株・後株の統一用）
LEGAL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("株式会社", "株式会社"),
    ("(株)", "株式会社"),
    ("（株）", "株式会社"),
    ("㈱", "株式会社"),
    ("有限会社", "有限会社"),
    ("(有)", "有限会社"),
    ("（有）", "有限会社"),
    ("㈲", "有限会社"),
    ("合同会社", "合同会社"),
    ("(同)", "合同会社"),
    ("（同）", "合同会社"),
    ("合資会社", "合資会社"),
    ("合名会社", "合名会社"),
    ("一般社団法人", "一般社団法人"),
    ("一般財団法人", "一般財団法人"),
    ("公益社団法人", "公益社団法人"),
    ("公益財団法人", "公益財団法人"),
    ("特定非営利活動法人", "特定非営利活動法人"),
    ("NPO法人", "特定非営利活動法人"),
    ("社会福祉法人", "社会福祉法人"),
    ("医療法人社団", "医療法人社団"),
    ("医療法人財団", "医療法人財団"),
    ("医療法人", "医療法人"),
    ("学校法人", "学校法人"),
    ("宗教法人", "宗教法人"),
)

PREFECTURE_RE = re.compile(
    r"^(北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|"
    r"埼玉県|千葉県|東京都|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|"
    r"岐阜県|静岡県|愛知県|三重県|滋賀県|京都府|大阪府|兵庫県|奈良県|和歌山県|"
    r"鳥取県|島根県|岡山県|広島県|山口県|徳島県|香川県|愛媛県|高知県|福岡県|"
    r"佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県)"
)

MUNICIPALITY_RE = re.compile(
    r"^([^\s]+?(?:市[^\s]+?区|市|区|町|村))"
)

KANJI_DIGIT_MAP: dict[str, int] = {
    "〇": 0,
    "零": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

HYPHEN_RE = re.compile(r"[−―‐ー－−]+")
CHOME_RE = re.compile(r"([0-9一二三四五六七八九十〇零]+)\s*(?:丁目|番地|番|号|条|線)")


@dataclass(frozen=True)
class NormalizedAddress:
    """Structured normalized Japanese address."""

    prefecture: str
    municipality: str
    normalized: str
    street_key: str


def compute_corporate_number_check_digit(body_12: str) -> str:
    """Compute the NTA 13th leading check digit from the 12-digit body."""
    if not body_12.isdigit() or len(body_12) != 12:
        raise ValueError(f"Corporate number body must be 12 digits: {body_12!r}")
    # 国税庁の公式検査数字式: 右端を桁位置1として偶数桁*2 + 奇数桁*1
    total = 0
    for idx, ch in enumerate(reversed(body_12), start=1):
        weight = 2 if idx % 2 == 0 else 1
        total += int(ch) * weight
    return str(9 - (total % 9))


def is_valid_corporate_number(corp_num: str) -> bool:
    """Validate a 13-digit Japanese Corporate Number (法人番号)."""
    cleaned = corp_num.strip()
    if not cleaned.isdigit() or len(cleaned) != 13:
        return False
    return cleaned[0] == compute_corporate_number_check_digit(cleaned[1:])


def _kanji_num_to_int(token: str) -> str:
    if token.isdigit():
        return token
    if "十" in token:
        parts = token.split("十", 1)
        tens = KANJI_DIGIT_MAP.get(parts[0], 1) if parts[0] else 1
        ones = KANJI_DIGIT_MAP.get(parts[1], 0) if parts[1] else 0
        return str(tens * 10 + ones)
    digits = [str(KANJI_DIGIT_MAP[c]) for c in token if c in KANJI_DIGIT_MAP]
    return "".join(digits) if len(digits) == len(token) else token


def normalize_company_name(name: str, strip_legal_form: bool = False) -> str:
    """Normalize Japanese company name (NFKC, kanji variants, legal prefix)."""
    text = unicodedata.normalize("NFKC", name).translate(VARIANT_KANJI_MAP)
    text = re.sub(r"\s+", "", text).upper()
    for raw_form, canonical in LEGAL_PREFIXES:
        canon_raw = unicodedata.normalize("NFKC", raw_form).upper()
        if canon_raw in text:
            text = text.replace(canon_raw, "" if strip_legal_form else canonical)
    return text


def normalize_address(raw_address: str) -> NormalizedAddress:
    """Normalize Japanese address into prefecture, municipality, and hyphenated street key."""
    text = unicodedata.normalize("NFKC", raw_address).translate(VARIANT_KANJI_MAP)
    text = re.sub(r"\s+", "", text)
    text = HYPHEN_RE.sub("-", text)

    prefecture = ""
    rest = text
    pref_match = PREFECTURE_RE.match(rest)
    if pref_match:
        prefecture = pref_match.group(1)
        rest = rest[len(prefecture) :]

    # 丁目・番地・号をハイフン区切りのアラビア数字に統一
    def _replace_chome(match: re.Match[str]) -> str:
        num = _kanji_num_to_int(match.group(1))
        return f"{num}-"

    rest = CHOME_RE.sub(_replace_chome, rest)
    rest = re.sub(r"-+", "-", rest).strip("-")

    municipality = ""
    muni_match = MUNICIPALITY_RE.match(rest)
    if muni_match:
        municipality = muni_match.group(1)

    normalized = f"{prefecture}{rest}"
    # ビル名・階数を落とした番地末尾までのコアキー（名寄せの頑健性向上）
    street_match = re.match(r"^([^\d-]+(?:\d+(?:-\d+)*)?)", rest)
    street_key = street_match.group(1) if street_match else rest
    return NormalizedAddress(
        prefecture=prefecture,
        municipality=municipality,
        normalized=normalized,
        street_key=f"{prefecture}{street_key}",
    )
