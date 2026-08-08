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


def dominant_lui(props: dict) -> str | None:
    """小地域集計型（lui_201… に面積が入る）から、最大面積の用途コードを選ぶ。

    1 ポリゴンが複数用途の面積を持つので単一区分にはならない。
    地図で塗り分けるために「主たる用途」を代表値として決める。
    集計値であること自体はビューア側で明示する。
    """
    best_code, best_area = None, 0.0
    for key, value in props.items():
        m = _LUI_RE.match(key)
        if not m:
            continue
        try:
            area = float(value or 0)
        except (TypeError, ValueError):
            continue
        # 231(不明) は主たる用途の候補にしない
        if m.group(1) == "231":
            continue
        if area > best_area:
            best_code, best_area = m.group(1), area
    return best_code if best_area > 0 else None


def annotate(props: dict, pref: str, reference: dict,
             field: str | None, is_national: bool = False) -> dict:
    """1 フィーチャの属性に lui_code / lui_name / lui_group を足して返す。

    優先順は (1) 国標準コードを持つ列があればそれ、(2) 県独自コードを対照表で写す、
    (3) 小地域集計型なら面積最大の用途を代表値にする。
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
    if code is None:
        code = dominant_lui(props)

    props = dict(props)
    props["lui_code"] = code or ""
    props["lui_name"] = reference["national_codes"].get(code, "") if code else ""
    props["lui_group"] = group_of(code) if code else GROUP_UNKNOWN
    return props
