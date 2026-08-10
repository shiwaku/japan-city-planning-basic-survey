"""土地利用レイヤに、全国で共通の用途コードを付ける。

自治体ごとに属性名も値の体系も違うので、そのままでは全国を同じ凡例で描けない。
国交省の対照表（codetable.py が解析）を使って、次の 2 つの属性を足す:

    lui_code   国標準の土地コード（201-253）。写せなければ空
    lui_name   その用途名（田・住宅用地 など）

## 3 系統は属性として持たない

自然的／都市的／低未利用の 3 系統は実施要領の章立てだが、地図の塗り分けには
使わない。実測で都市的土地利用が 75.9% を占め、市街地がほぼ一色になって
「住宅か商業か工場か」という肝心の情報が消えるため。ビューアは lui_code の
20 区分で塗る。系統は normalize の集計表示にだけ使い、lui_code から求まるので
属性としては書かない（group_of）。

## 写せなかったものを消さない

対照表は公式だが実データを完全にはカバーしない。さいたま市の 141-144・150 は
埼玉県のシートに載っておらず、実測で 12.6% が写せなかった。
これらは lui_code="" のまま残し、ビューア側では色ではなくハッチで描く。
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
#   LU_1             東京都。LU_1〜LU_4 と段階があり、LU_1 が主用途
LOCAL_CODE_FIELDS = ("LANDUSE", "LANDUSE_", "landuse", "tochiriyou",
                     "土地コード", "LU_1", "lu_code", "youto", "YOUTO")
NATIONAL_CODE_FIELDS = ("国コード", "lui_code_national", "national_code")


def _clean(name: str) -> str:
    """属性名の照合用。制御文字と前後の空白を落とす（埼玉県は 'LANDUSE\\r'）。"""
    return name.strip().strip("\r\n\t ")

# 小地域集計型の列名（lui_201 など）。この型は用途コード列を持たない
_LUI_RE = re.compile(r"^lui_(\d{3})$")

# 集計値が列で横に並ぶ型。lui_201 のように国標準コードを列名にするものだけでなく、
# 通し番号や別の体系で並べるものもある。実測で見つかった型:
#
#   LUI_1 … LUI_16          用途別土地利用面積（埼玉県ほか。1〜2 桁なので _LUI_RE を素通りする）
#   B_AGE_1 … B_AGE_999     建築年別の棟数
#   B_FLR_*, B_BFA_*        階数別・建築面積規模別の棟数
#   b_use_401 …             建物用途別の棟数
#   b_area_701 …, b_fl_a_801 …   用途別の建築面積・延床面積
#
# いずれも 1 ポリゴン = 1 小地域で、地物の形と属性の意味が対応しない。
_WIDE_RE = re.compile(r"^(lui|bui|b_use|b_age|b_flr|b_bfa|b_area|b_fl_a)_?\d+$",
                      re.IGNORECASE)

# 何列並んでいれば「横に並べた集計」と見なすか。1 列だけなら単なる属性名の
# 可能性があるので、敷地ベースのレイヤを巻き込まないよう 3 列以上を条件にする
_WIDE_MIN = 3


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

    判定は 3 つ。どれか 1 つでも当たれば集計型:

      1. small_area と area_id_no を両方持つ（大分県ほか）
      2. lui_201 のように国標準コードを列名にした列がある
      3. 集計値を横に並べた列が 3 列以上ある（LUI_1…、B_AGE_1… など）
    """
    names = {_clean(f) for f in fields}
    if all(m in names for m in AGGREGATE_MARKERS):
        return True
    if any(_LUI_RE.match(n) for n in names):
        return True
    return sum(1 for n in names if _WIDE_RE.match(n)) >= _WIDE_MIN


ANNOTATED_FIELDS = ("lui_code", "lui_name", "lui_group")


def strip_annotation(props: dict) -> dict:
    """annotate が足した属性を落とす。

    集計型と判定する前に注釈してしまった名残を消すため。付いたままだと
    「1 ポリゴン = 小地域まるごと」に用途 1 つが割り当たった状態になり、
    ビューアが敷地ベースと同じ色で塗ってしまう。
    """
    return {k: v for k, v in props.items() if k not in ANNOTATED_FIELDS}


def non_parcel_reason(geometry: str, fields: list[str]) -> str | None:
    """敷地ベースの土地利用でないなら、その理由を返す。該当すれば None。

    土地利用現況は「1 ポリゴン = 1 敷地、属性に用途」が本来の姿。ところが
    調査項目名での振り分けでは、同じ「土地利用」に別物が入ってくる:

        点            開発許可・大規模事業所などの位置
        用途コード列なし  町丁目単位の集計、市街化区域界、DID、既成市街地界
        小地域集計型     用途別の面積が lui_201… と列で並ぶ（形と属性が対応しない）

    どれも敷地の用途は表さないので、同じ凡例では読めない。
    """
    if not geometry.startswith(("Polygon", "MultiPolygon")):
        return f"ポリゴンでない({geometry or '不明'})"
    if is_aggregate(fields):
        return "小地域集計型"
    if code_field(fields)[0] is None:
        return "用途コード列なし"
    return None


def annotate(props: dict, pref: str, reference: dict,
             field: str | None, is_national: bool = False) -> dict:
    """1 フィーチャの属性に lui_code / lui_name を足して返す。

    優先順は (1) 国標準コードを持つ列があればそれ、(2) 県独自コードを対照表で写す。

    前回の実行が付けた注釈は落としてから付け直す。付け方を変えたときに
    古い属性（lui_group など）が残らないようにするため。

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
    props = strip_annotation(props)
    props["lui_code"] = code or ""
    props["lui_name"] = reference["national_codes"].get(code, "") if code else ""
    return props
