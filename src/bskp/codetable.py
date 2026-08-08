"""都道府県ごとの土地利用コードを、国土交通省の標準コードに写像する対照表を読む。

## なぜ要るか

都市計画基礎調査のデータは、自治体ごとに属性名も値の体系も揃っていない。
実測した土地利用レイヤは、そもそもデータモデルが 2 系統に割れていた:

  A) `LANDUSE` に用途コードが 1 つ入る「区分図」型（さいたま市の実値は 10,20,…,150）
  B) `lui_201`〜`lui_253` に用途別の面積が並ぶ「小地域集計」型（国標準の列名）

A のコード体系は都道府県ごとに独自で、そのままでは全国を同じ凡例で描けない。
国土交通省が 47 都道府県分の対照表を公開しているので、それを機械可読にする。

  コード表     https://www.mlit.go.jp/toshi/city_plan/content/001406903.xlsx
  対照表       https://www.mlit.go.jp/toshi/city_plan/content/001406905.xlsx
  （出典ページ https://www.mlit.go.jp/toshi/city_plan/toshi_city_plan_tk_000049.html ）

## XLSX の構造（実測）

47 シート（`11_埼玉県 土地` のような名前）が同じ形をしている:

  2 行目  B列 "国出典：" / I列 "<県名>出典："  ← ここで国側と県側が分かれる
  3 行目  見出し。両側に "用途区分" と "土地コード" がある
  4 行目〜 データ。結合セルで空欄になる箇所があるので前方補完する

セル文字列にはルビ（rPh）が混ざる。`田タ` のようになるので本文だけ取り出す。
openpyxl を使わず標準の zipfile + ElementTree で読む（依存を増やさないため）。
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

CODE_TABLE_URL = "https://www.mlit.go.jp/toshi/city_plan/content/001406903.xlsx"
CROSSWALK_URL = "https://www.mlit.go.jp/toshi/city_plan/content/001406905.xlsx"

_SHEET_RE = re.compile(r"^(\d{2})_(.+?)\s*土地")
_COL_RE = re.compile(r"([A-Z]+)(\d+)")


def _col_index(ref: str) -> int:
    """セル参照 'AB12' の列を 0 始まりの番号にする。"""
    letters = _COL_RE.match(ref).group(1)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    """共有文字列。ルビ（rPh 配下の t）は本文ではないので除く。"""
    out: list[str] = []
    if "xl/sharedStrings.xml" not in z.namelist():
        return out
    for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(NS + "si"):
        ruby = {t for ph in si.iter(NS + "rPh") for t in ph.iter(NS + "t")}
        out.append("".join(t.text or "" for t in si.iter(NS + "t") if t not in ruby))
    return out


def _sheets(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels}
    return [(s.get("name"), target[s.get(REL + "id")]) for s in wb.iter(NS + "sheet")]


def _rows(sheet: ET.Element, strings: list[str]) -> list[dict[int, str]]:
    """行ごとに {列番号: 値} を返す。空セルは含めない。"""
    out = []
    for row in sheet.iter(NS + "row"):
        cells: dict[int, str] = {}
        for c in row.iter(NS + "c"):
            v = c.find(NS + "v")
            if v is None or v.text is None:
                continue
            raw = strings[int(v.text)] if c.get("t") == "s" else v.text
            text = (raw or "").strip()
            if text:
                cells[_col_index(c.get("r"))] = text
        out.append(cells)
    return out


@dataclass
class LandUseCrosswalk:
    """1 都道府県分の対応表。"""
    pref_code: str
    pref_name: str
    source: str = ""
    #  県コード -> 国標準コード
    to_national: dict[str, str] = field(default_factory=dict)
    #  県コード -> 県側の用途区分名（検証と凡例用に残す）
    local_name: dict[str, str] = field(default_factory=dict)


# 国標準コードは 201-205（自然的）、211-223（都市的）、231、253
_NATIONAL_RE = re.compile(r"^(20[1-5]|21[1-9]|22[0-3]|231|253)$")
# 県コードは体系がまちまちなので「数字だけ」しか仮定しない
_LOCAL_RE = re.compile(r"^\d{1,4}$")


@dataclass
class Columns:
    pref_start: int
    natl_name: int
    natl_code: int
    local_name: int
    #  県側の土地コード列。主区分と細分の 2 本ある県がある（埼玉県は 60 と 61-63 の両方が
    #  実データに出てくるので、どちらも国コードに写像できないと取りこぼす）。
    local_codes: list[int]


def _norm(s: str) -> str:
    """見出し照合用。改行や空白の混入で一致しなくなるのを防ぐ（北海道は "土地\\nコード"）。"""
    return re.sub(r"\s+", "", s)


def _find_columns(rows: list[dict[int, str]]) -> Columns | None:
    pref_start = None
    for cells in rows[:6]:
        for col, val in sorted(cells.items()):
            if "出典" in val and not val.startswith("国"):
                pref_start = col
                break
        if pref_start is not None:
            break
    if pref_start is None:
        return None

    for cells in rows[:8]:
        left: dict[str, int] = {}
        right_name: int | None = None
        right_codes: list[int] = []
        for col in sorted(cells):
            key = _norm(cells[col])
            if col < pref_start:
                left.setdefault(key, col)
            elif key == "用途区分" and right_name is None:
                right_name = col
            elif key == "土地コード":
                right_codes.append(col)
        if "用途区分" in left and "土地コード" in left and right_name is not None and right_codes:
            return Columns(pref_start, left["用途区分"], left["土地コード"],
                           right_name, right_codes)
    return None


def parse_sheet(name: str, rows: list[dict[int, str]]) -> LandUseCrosswalk | None:
    m = _SHEET_RE.match(name)
    if not m:
        return None
    cw = LandUseCrosswalk(pref_code=m.group(1), pref_name=m.group(2).strip())

    cols = _find_columns(rows)
    if cols is None:
        log.warning("%s: 見出し行を特定できませんでした", name)
        return None

    for cells in rows[:4]:
        for col, val in cells.items():
            if col >= cols.pref_start and "出典" not in val and len(val) > 4:
                cw.source = val
                break
        if cw.source:
            break

    # 国側は結合セルで縦に伸びるので前方補完する。
    # 県側のコードは補完しない——空欄は「この行に県コードが無い」であって、
    # 上の行の値が続いているわけではない（補完すると 1 つ上の用途に取り違える）。
    natl_code = natl_name = local_name = ""
    for cells in rows:
        natl_code = cells.get(cols.natl_code, "") or natl_code
        natl_name = cells.get(cols.natl_name, "") or natl_name
        local_name = cells.get(cols.local_name, "") or local_name
        if not _NATIONAL_RE.match(natl_code):
            continue

        for col in cols.local_codes:
            local_code = cells.get(col, "")
            # "-" は「県独自コードを持たず国標準をそのまま使う」の意味。写像は不要
            if not _LOCAL_RE.match(local_code):
                continue
            # 同じ県コードが複数の国コードに現れたら最初の対応を採る
            if local_code not in cw.to_national:
                cw.to_national[local_code] = natl_code
                cw.local_name[local_code] = local_name or natl_name
    return cw


def parse_crosswalk(path: Path) -> list[LandUseCrosswalk]:
    """対照表 XLSX を読んで、都道府県ごとの対応表を返す。"""
    with zipfile.ZipFile(path) as z:
        strings = _shared_strings(z)
        result = []
        for name, target in _sheets(z):
            sheet = ET.fromstring(z.read("xl/" + target.lstrip("/")))
            cw = parse_sheet(name, _rows(sheet, strings))
            if cw is None:
                continue
            result.append(cw)
            if not cw.to_national:
                # 県独自コードを持たず国標準をそのまま使う県（大分・大阪など）は
                # 対応が 0 件で正常。取りこぼしと区別できるよう情報として出す。
                log.info("%s: 県独自コードなし（国標準をそのまま使用）", name)
    return result


def parse_code_table(path: Path) -> dict[str, str]:
    """コード表 XLSX から 国標準コード -> 用途名 を読む。"""
    codes: dict[str, str] = {}
    with zipfile.ZipFile(path) as z:
        strings = _shared_strings(z)
        for name, target in _sheets(z):
            if "土地用途コード表" not in name:
                continue
            sheet = ET.fromstring(z.read("xl/" + target.lstrip("/")))
            for cells in _rows(sheet, strings):
                vals = [cells[c] for c in sorted(cells)]
                for i, v in enumerate(vals):
                    if _NATIONAL_RE.match(v) and i > 0:
                        codes[v] = vals[i - 1]
                        break
    return codes


def build_reference(crosswalk: Path, code_table: Path) -> dict:
    """対照表とコード表をまとめて、正規化に使える 1 つの辞書にする。"""
    codes = parse_code_table(code_table)
    walks = parse_crosswalk(crosswalk)
    return {
        "national_codes": codes,
        "source": {
            "code_table": CODE_TABLE_URL,
            "crosswalk": CROSSWALK_URL,
            "page": "https://www.mlit.go.jp/toshi/city_plan/toshi_city_plan_tk_000049.html",
        },
        "prefectures": {
            cw.pref_name: {
                "pref_code": cw.pref_code,
                "source": cw.source,
                "to_national": cw.to_national,
                "local_name": cw.local_name,
            }
            for cw in walks
        },
    }


def normalize_code(reference: dict, pref_name: str, local_code: str) -> str | None:
    """県独自コードを国標準コードに写す。写せなければ None。

    上位桁からの推測はしない。埼玉県では 90 が農林漁業施設用地(219) なのに
    91-96 は公益施設用地(214) で、桁を落とす推測が実データで成り立たないため。
    """
    entry = (reference.get("prefectures") or {}).get(pref_name)
    if not entry:
        return None
    code = str(local_code).strip()
    if code in entry["to_national"]:
        return entry["to_national"][code]
    # 県独自コードを持たない県は、データが最初から国標準コードで入っている
    if not entry["to_national"] and code in reference["national_codes"]:
        return code
    return None
