"""建物レイヤに、全国で共通の用途コードと高さを付ける。

土地利用（normalize.py）と同じ考え方だが、事情がひとつ違う。

## 建物には全国対照表が無い

国交省の対照表 XLSX は 47 シートすべてが「土地」で、建物版が無い。
コード表（C0401建物用途コード表）には国標準の 401-471 が定義されているので、
**publisher ごとの対照は自前で作る**しかない。作る根拠は各自治体の定義書:

    東京都    令和３年度区部土地利用現況調査_データベース定義書.pdf
              BV_6 建物用途分類コード（111-150）＋「建物用途コード表」
    さいたま市  GISデータ_DB定義書_建物現況調査.pdf
              RIYOU 建物用途（1-27, 88）＋「別表１ 建物用途コード」

国標準のほうが区分が粗いので、写すと情報が落ちる方向になる（東京都の
112 教育文化施設と 113 厚生医療施設は、どちらも 422 文教厚生施設になる）。
逆向きの取りこぼしは無い。判断が入った箇所は下のコメントに残す。

## 高さは実測値だけを使う

東京都は BV_15 建築物の高さ（m）、さいたま市は TAKASA（m）を持つ。
実測で東京都 R03 の 99.8% に有効値があり、中央値 6.7m・最大 144.7m だった。
階数×階高で推定すれば全レイヤに高さを付けられるが、やらない。
実測値と推定値が同じ属性に混ざると、見た目からは区別できなくなる。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 国標準の建物用途（国交省 C0401建物用途コード表）
USES = {
    "401": "業務施設",
    "402": "商業施設",
    "403": "宿泊施設",
    "404": "商業系用途複合施設",
    "411": "住宅",
    "412": "共同住宅",
    "413": "店舗等併用住宅",
    "414": "店舗等併用共同住宅",
    "415": "作業所併用住宅",
    "421": "官公庁施設",
    "422": "文教厚生施設",
    "431": "運輸倉庫施設",
    "441": "工場",
    "451": "農林漁業用施設",
    "452": "供給処理施設",
    "453": "防衛施設",
    "454": "その他",
    "461": "不明",
    "471": "空家",
}

# 東京都 BV_6（建物用途分類コード）→ 国標準。
# 出典: 令和３年度区部土地利用現況調査_データベース定義書「建物用途コード表」
#
# 判断が入った箇所:
#   123 住商併用建物   独立/集合の別を持たないので 413 と 414 を区別できない。413 に寄せた
#   124 宿泊・遊興施設  細分類 BV_7 で 1=宿泊 / 2=遊興 に分かれる。BV_7 を見て振り分ける
#   125 スポーツ・興行施設  定義書の区分欄で 121-125 は「商業用地」に属するので 402 とした
TOKYO_BV6 = {
    "111": "421",   # 官公庁施設
    "112": "422",   # 教育文化施設 -> 文教厚生施設
    "113": "422",   # 厚生医療施設 -> 文教厚生施設
    "114": "452",   # 供給処理施設
    "121": "401",   # 事務所建築物 -> 業務施設
    "122": "402",   # 専用商業施設 -> 商業施設
    "123": "413",   # 住商併用建物 -> 店舗等併用住宅
    "124": "403",   # 宿泊・遊興施設 -> 宿泊施設（遊興は BV_7 で 402 に振る）
    "125": "402",   # スポーツ・興行施設 -> 商業施設
    "131": "411",   # 独立住宅 -> 住宅
    "132": "412",   # 集合住宅 -> 共同住宅
    "141": "441",   # 専用工場 -> 工場
    "142": "415",   # 住居併用工場 -> 作業所併用住宅
    "143": "431",   # 倉庫運輸関係施設 -> 運輸倉庫施設
    "150": "451",   # 農林漁業施設 -> 農林漁業用施設
}

# さいたま市 RIYOU（建物用途）→ 国標準。
# 出典: GISデータ_DB定義書_建物現況調査.pdf「別表１ 建物用途コード」
#
# 判断が入った箇所:
#   3  商業・業務併用住宅  独立/集合の別が無いので 413（414 と区別できない）
#   12 風俗営業施設 / 13 娯楽施設 / 14,15 遊戯施設  いずれも商業施設 402 に寄せた
#   88 建物としてカウントしない構造物等  定義書が「建物ではない」と言っているので写さない
SAITAMA_RIYOU = {
    "1": "411",    # 専用住宅
    "2": "412",    # 共同住宅
    "3": "413",    # 商業・業務併用住宅
    "4": "415",    # 工業併用住宅 -> 作業所併用住宅
    "5": "402", "6": "402", "7": "402", "8": "402",   # 商業施設(A)-(D)
    "9": "401",    # 業務施設
    "10": "404",   # 商業・業務施設 -> 商業系用途複合施設
    "11": "403",   # 宿泊施設
    "12": "402", "13": "402", "14": "402", "15": "402",  # 風俗営業・娯楽・遊戯
    "16": "421",   # 官公庁施設
    "17": "422", "18": "422", "19": "422",  # 文教厚生施設(A)-(C)
    "20": "422",   # 医療・福祉施設 -> 文教厚生施設
    "21": "452",   # 供給処理施設
    "22": "441", "23": "441",   # 工業施設(A)(B) -> 工場
    "24": "431", "25": "431",   # 運輸・倉庫施設(A)(B)
    "26": "451",   # 農林漁業施設
    "27": "454",   # その他
}

# 建物としてカウントしない印。写さないだけでなく、地図にも出さない
SAITAMA_NOT_A_BUILDING = "88"

# 島田市 YOUTO（建物用途コード）→ 国標準。
# 出典: 配布物に同梱の「島田市データベース定義書.xlsx」シート『コード表1』
#       （都市計画基礎調査 建物用途 1：建物用途コード表）
#
# さいたま市の RIYOU と似た 1 始まりの連番だが別体系。あちらは 27 区分、
# こちらは 32 区分で、途中の並びも違う。列名で写像を選ぶので混ざらない。
#
# 判断が入った箇所:
#   4,5,6 店舗併用共同住宅(A)(B)(C)  国標準は A/B/C の別を持たないので 414 にまとめた
#   13-17 娯楽施設・遊技施設        商業施設 402 に寄せた（さいたま市の前例に合わせる）
#   29,30 危険物貯蔵・処理施設(A)(B)  国標準に対応する区分が無い。コード表では工業系
#                                （22-28）の外、農林漁業用施設の手前に置かれており、
#                                供給処理施設（上下水道・清掃・変電）とも違うので
#                                その他 454 とした
SHIMADA_YOUTO = {
    "1": "411",    # 住宅
    "2": "412",    # 共同住宅
    "3": "413",    # 店舗併用住宅
    "4": "414", "5": "414", "6": "414",   # 店舗併用共同住宅(A)-(C)
    "7": "415",    # 作業所併用住宅
    "8": "401",    # 業務施設
    "9": "402", "10": "402", "11": "402",  # 商業施設(A)-(C)
    "12": "403",   # 宿泊施設
    "13": "402", "14": "402", "15": "402",  # 娯楽施設(A)-(C)
    "16": "402", "17": "402",              # 遊技施設(A)(B)
    "18": "404",   # 商業系用途複合施設
    "19": "421",   # 官公庁施設
    "20": "422", "21": "422",   # 文教厚生施設(A)(B)
    "22": "431", "23": "431",   # 運輸倉庫施設(A)(B)
    "24": "441",   # 重工業施設
    "25": "441",   # 軽工業施設
    "26": "441", "27": "441",   # サービス工業施設(A)(B)
    "28": "441",   # 家内工業施設
    "29": "454", "30": "454",   # 危険物貯蔵・処理施設(A)(B)
    "31": "451",   # 農林漁業用施設
    "32": "454",   # その他
}

# publisher ごとの用途コード列。値の体系ごと違うので、列名で写像を選ぶ
CROSSWALKS: dict[str, dict[str, str]] = {
    "BV_6": TOKYO_BV6,
    "RIYOU": SAITAMA_RIYOU,
    "YOUTO": SHIMADA_YOUTO,
}

# 実測の高さ（m）。推定値は入れない
HEIGHT_FIELDS = ("BV_15", "TAKASA")
# 地上階数
FLOOR_FIELDS = ("BV_3", "KAISU")

# 欠測を表す番兵。東京都は -999 を使う（実測で 0.2%）
_SENTINEL = -900.0


def _clean(name: str) -> str:
    return name.strip().strip("\r\n\t ")


def code_field(fields: list[str]) -> str | None:
    """用途コードの列名を返す。対照表を持たない publisher なら None。"""
    lookup = {_clean(f): f for f in fields}
    for name in CROSSWALKS:
        if name in lookup:
            return lookup[name]
    return None


def _pick(props: dict, names: tuple[str, ...]) -> tuple[str | None, object]:
    lookup = {_clean(k): k for k in props}
    for name in names:
        if name in lookup:
            return lookup[name], props[lookup[name]]
    return None, None


def _number(value: object) -> float | None:
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if n <= _SENTINEL else n


def non_building_reason(geometry: str, fields: list[str]) -> str | None:
    """建物ベースのレイヤでないなら、その理由を返す。該当すれば None。

    土地利用と同じで、調査項目名での振り分けには別物が混ざる。建物は
    「1 ポリゴン = 1 棟、属性に用途」が本来の姿。
    """
    from .normalize import is_aggregate

    if not geometry.startswith(("Polygon", "MultiPolygon")):
        return f"ポリゴンでない({geometry or '不明'})"
    if is_aggregate(fields):
        return "小地域集計型"
    if code_field(fields) is None:
        return "用途コード列なし"
    return None


ANNOTATED_FIELDS = ("bui_code", "bui_name", "bui_height", "bui_floors")


def strip_annotation(props: dict) -> dict:
    return {k: v for k, v in props.items() if k not in ANNOTATED_FIELDS}


def annotate(props: dict, field: str) -> dict:
    """1 棟の属性に bui_code / bui_name と、あれば bui_height / bui_floors を足す。

    高さは実測値がある publisher にだけ付く。無い場合は属性ごと付けない
    （0 を入れると「高さ 0m の建物」になってしまう）。
    """
    table = CROSSWALKS[_clean(field)]
    raw = props.get(field)
    text = "" if raw in (None, "") else str(raw).strip().split(".")[0]
    code = table.get(text, "")

    props = strip_annotation(props)
    props["bui_code"] = code
    props["bui_name"] = USES.get(code, "")

    _, height = _pick(props, HEIGHT_FIELDS)
    value = _number(height)
    if value is not None and value > 0:
        props["bui_height"] = round(value, 1)
    _, floors = _pick(props, FLOOR_FIELDS)
    value = _number(floors)
    if value is not None and value > 0:
        props["bui_floors"] = int(value)
    return props


def is_not_a_building(props: dict, field: str) -> bool:
    """publisher 自身が「建物ではない」と印を付けているものか。"""
    if _clean(field) != "RIYOU":
        return False
    raw = props.get(field)
    text = "" if raw in (None, "") else str(raw).strip().split(".")[0]
    return text == SAITAMA_NOT_A_BUILDING
