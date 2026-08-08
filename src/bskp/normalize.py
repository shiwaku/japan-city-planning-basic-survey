"""土地利用レイヤに、全国で共通の用途コードを付ける。

自治体ごとに属性名も値の体系も違うので、そのままでは全国を同じ凡例で描けない。
国交省の対照表（codetable.py が解析）を使って、次の 3 つの属性を足す:

    lui_code   国標準の土地コード（201-253）。写せなければ空
    lui_name   その用途名（田・住宅用地 など）
    lui_group  3 系統への集約（自然的土地利用 / 都市的土地利用 / 低未利用土地）

## なぜ 3 系統に束ねるか

国標準の用途は 20 区分あるが、20 色のカテゴリカル配色は検証を通せない。
地図の重ね合わせは「全ペアが同時に隣接しうる」条件になり、検証済み 8 色でも
緑↔橙が CVD ΔE 3.2、赤↔橙が通常視 ΔE 7.1 で落ちる。3 色なら両モードで通る。

## 写せなかったものを消さない

対照表は公式だが実データを完全にはカバーしない。さいたま市の 141-144・150 は
埼玉県のシートに載っておらず、実測で 12.6% が写せなかった。
これらは lui_group="未分類" として残す。ビューア側では色ではなくハッチで描く
（4 色目の中立色はダークモードで aqua と ΔE 13.3 まで近づき、分離できないため）。
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# 国標準コード -> 3 系統。実施要領の章立て（自然的土地利用／都市的土地利用／低未利用土地）に従う
NATURAL = {"201", "202", "203", "204", "205"}
URBAN = {"211", "212", "213", "214", "215", "216", "217", "218", "219",
         "220", "221", "222", "223"}
UNDERUSED = {"253"}

GROUP_NATURAL = "自然的土地利用"
GROUP_URBAN = "都市的土地利用"
GROUP_UNDERUSED = "低未利用土地"
GROUP_UNKNOWN = "未分類"

GROUPS = [GROUP_NATURAL, GROUP_URBAN, GROUP_UNDERUSED, GROUP_UNKNOWN]

# 用途コードが入っている属性名。publisher ごとにまったく揃っていないので候補を並べる。
# 実測で見つかった名前（すべて土地利用現況の同じ意味の列）:
#   LANDUSE          埼玉県。ただし DBF のフィールド名に \r が混入していて
#                    そのままでは一致しない（名前は正規化してから照合する）
#   tochiriyou       静岡県
#   土地コード        津島市（愛知県）
#   国コード          津島市。こちらは最初から国標準コードなので写像不要
LOCAL_CODE_FIELDS = ("LANDUSE", "LANDUSE_", "landuse", "tochiriyou",
                     "土地コード", "lu_code", "youto", "YOUTO")
NATIONAL_CODE_FIELDS = ("国コード", "lui_code_national", "national_code")


def _clean(name: str) -> str:
    """属性名の照合用。制御文字と前後の空白を落とす（埼玉県は 'LANDUSE\\r'）。"""
    return name.strip().strip("\r\n\t ")

# 小地域集計型の列名（lui_201 など）。この型は用途コード列を持たない
_LUI_RE = re.compile(r"^lui_(\d{3})$")


def group_of(code: str) -> str:
    if code in NATURAL:
        return GROUP_NATURAL
    if code in URBAN:
        return GROUP_URBAN
    if code in UNDERUSED:
        return GROUP_UNDERUSED
    return GROUP_UNKNOWN


def code_field(fields: list[str]) -> tuple[str | None, bool]:
    """(実際の属性名, それが国標準コードか) を返す。見つからなければ (None, False)。

    属性名は publisher ごとに違い、制御文字が混ざることもあるので正規化して照合する。
    """
    lookup = {_clean(f): f for f in fields}
    for name in NATIONAL_CODE_FIELDS:
        if name in lookup:
            return lookup[name], True
    for name in LOCAL_CODE_FIELDS:
        if name in lookup:
            return lookup[name], False
    return None, False


# 小地域集計型の目印。small_area / area_id_no を持ち、用途別の面積や棟数が
# lui_201… bui_401… のように列で並ぶ。1 ポリゴン = 1 小地域なので、
# 地物の形と属性の意味が対応しない
AGGREGATE_MARKERS = ("small_area", "area_id_no")


def is_aggregate(fields: list[str]) -> bool:
    """小地域集計型なら True。

    土地利用は敷地ベース、建物は建物ベースのポリゴンが本来の姿で、
    集計型は 1 ポリゴンが小地域まるごとを指す別物。同じ地図記号では描けない。
    実測では土地利用の 99.8%（984,681/986,643 フィーチャ）が個別ポリゴンで、
    集計型は 0.2% しかない。混ぜると凡例の意味が壊れるので分けて扱う。
    """
    names = {_clean(f) for f in fields}
    if all(m in names for m in AGGREGATE_MARKERS):
        return True
    return any(_LUI_RE.match(n) for n in names)


def annotate(props: dict, pref: str, reference: dict,
             field: str | None, is_national: bool = False) -> dict:
    """1 フィーチャの属性に lui_code / lui_name / lui_group を足して返す。

    優先順は (1) 国標準コードを持つ列があればそれ、(2) 県独自コードを対照表で写す。

    小地域集計型からの「主たる用途」の導出はしない。1 ポリゴンが小地域まるごとで
    複数用途の面積を持つため、代表値を1つ選ぶと元データに無い値を作ることになる。
    集計型のレイヤ自体を対象から外す（is_aggregate で判定）。
    """
    from .codetable import normalize_code

    code = None
    raw = props.get(field) if field else None
    if raw not in (None, ""):
        text = str(raw).strip()
        # 数値として入っていると 211.0 のようになるので整数部だけ見る
        text = text.split(".")[0]
        if is_national:
            code = text if text in reference["national_codes"] else None
        else:
            code = normalize_code(reference, pref, text)
    props = dict(props)
    props["lui_code"] = code or ""
    props["lui_name"] = reference["national_codes"].get(code, "") if code else ""
    props["lui_group"] = group_of(code) if code else GROUP_UNKNOWN
    return props
