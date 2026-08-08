"""カタログを横断検索して、都市計画基礎調査データセットのインベントリを作る。

出力は 2 種類:
  data/inventory/datasets.jsonl  … CKAN のメタデータをほぼそのまま（1 行 1 データセット）
  data/inventory/resources.csv   … 1 行 1 リソース。ダウンローダの入力になる
"""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .ckan import Catalog, CkanClient

log = logging.getLogger(__name__)

# 検索クエリ。インスタンスによってフレーズ検索の効き方が違うので複数投げて OR で束ねる
QUERIES = [
    "都市計画基礎調査",
    '"都市計画基礎調査"',
    "土地利用現況調査",
    "建物利用現況調査",
]

# クライアント側フィルタ。タイトル・説明・タグのどれかに現れれば採用。
# 「都市計画基礎調査」を名乗らない出し方が実在するので 2 段構えにする。
# 東京都は同じものを「土地利用現況調査GISデータ」として公開しており、
# 説明文で「都市計画法第６条の規定に基づく都市計画に関する基礎調査の一つ」と述べている。
STRICT_PATTERNS = [
    re.compile(r"都市計画基礎調査"),
    re.compile(r"都市計画に関する基礎調査"),
]

# 調査項目名だけで公開されているケース。実施要領の調査項目名そのものなので
# 単独でも特異性は十分あると判断し、文脈条件は課さない。
# （東京都「土地利用現況調査GISデータ」はメタデータに「都市計画」の語が一切なく、
#   説明は「東京都における土地利用現況調査の結果をGISデータで作成したものです」だけ。
#   文脈を要求すると取りこぼす。）
# 誤検出を後から外せるよう、この経路で拾ったものは match="alias" として記録する。
ALIAS_PATTERNS = [
    re.compile(r"土地利用現況調査"),
    re.compile(r"建物利用現況調査"),
    re.compile(r"都市計画基礎データ"),
]

# 調査項目の分類。実施要領の章立て（人口/産業/土地利用/建物/都市施設/地価/自然環境/
# 災害/景観）に寄せている。1 リソースが複数該当することがあるのでリストで持つ
THEME_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("人口", re.compile(r"人口|世帯|通勤|通学|流動")),
    ("産業", re.compile(r"産業|事業所|従業者|商業|工業|農業")),
    ("土地利用", re.compile(r"土地利用|宅地開発|農地|未利用地|市街地|開発許可|land")),
    ("建物", re.compile(r"建物|建築|空き家|延床|階数|構造|耐火")),
    ("都市施設", re.compile(r"都市施設|道路|公園|下水|交通")),
    ("地価", re.compile(r"地価|公示|路線価")),
    ("自然環境", re.compile(r"自然環境|緑地|水系|地形")),
    ("災害", re.compile(r"災害|公害|防災|浸水|土砂")),
    ("景観", re.compile(r"観光|景観|歴史|文化財")),
]

# 空間データを含みうる形式。ZIP は中身を開くまで分からないので「要確認」扱い
GEO_FORMATS = {"SHP", "SHAPE", "GEOJSON", "JSON", "KML", "KMZ", "GML", "GPKG",
               "FGB", "MBTILES", "PMTILES", "DXF", "DWG"}
ARCHIVE_FORMATS = {"ZIP", "7Z", "LZH", "TAR", "GZ"}
TABULAR_FORMATS = {"CSV", "TSV", "XLS", "XLSX", "ODS"}


@dataclass
class ResourceRow:
    catalog_id: str
    catalog_name: str
    organization: str
    dataset_name: str
    dataset_title: str
    dataset_url: str
    license: str
    resource_id: str
    resource_name: str
    format: str
    size: str
    themes: str
    kind: str
    match: str
    url: str


def _text_of(pkg: dict) -> str:
    parts = [pkg.get("title") or "", pkg.get("name") or "", pkg.get("notes") or ""]
    parts += [t.get("display_name") or t.get("name") or "" for t in pkg.get("tags") or []]
    for grp in pkg.get("groups") or []:
        parts.append(grp.get("display_name") or grp.get("title") or "")
    return "\n".join(parts)


def match_kind(pkg: dict) -> str:
    """"strict" / "alias" / "" を返す。"" は対象外。

    alias 判定は resources.csv の match 列に残すので、後段で緩い一致だけを
    外して集計し直せる。
    """
    text = _text_of(pkg)
    if any(p.search(text) for p in STRICT_PATTERNS):
        return "strict"
    if any(p.search(text) for p in ALIAS_PATTERNS):
        return "alias"
    return ""


def themes_for(*texts: str) -> list[str]:
    blob = " ".join(t for t in texts if t)
    return [name for name, pat in THEME_PATTERNS if pat.search(blob)]


def classify_format(fmt: str) -> str:
    f = (fmt or "").strip().upper()
    if f in GEO_FORMATS:
        return "geo"
    if f in ARCHIVE_FORMATS:
        return "archive"      # 中身に SHP が入っている可能性が高い。展開してから判定
    if f in TABULAR_FORMATS:
        return "tabular"
    if f in {"PDF", "HTML", "DOC", "DOCX"}:
        return "document"
    return "other"


def harvest_catalog(cat: Catalog, limit: int | None = None) -> tuple[list[dict], list[ResourceRow]]:
    client = CkanClient(cat)
    seen: dict[str, dict] = {}
    for q in QUERIES:
        try:
            for pkg in client.search(q, limit=limit):
                seen.setdefault(pkg["id"], pkg)
        except Exception as exc:  # noqa: BLE001 - 1 カタログの失敗で全体を止めない
            log.warning("%s: query %r failed: %s", cat.id, q, exc)

    matched = [(p, m) for p in seen.values() if (m := match_kind(p))]
    datasets = [p for p, _ in matched]
    n_alias = sum(1 for _, m in matched if m == "alias")
    log.info("%s: %d fetched -> %d relevant (strict %d / alias %d)",
             cat.id, len(seen), len(datasets), len(datasets) - n_alias, n_alias)

    rows: list[ResourceRow] = []
    for pkg, match in matched:
        org = (pkg.get("organization") or {}).get("title") or ""
        ds_url = pkg.get("url") or cat.dataset_url(pkg.get("name", ""))
        for res in pkg.get("resources") or []:
            fmt = (res.get("format") or "").strip().upper()
            rows.append(ResourceRow(
                catalog_id=cat.id,
                catalog_name=cat.name,
                organization=org,
                dataset_name=pkg.get("name", ""),
                dataset_title=pkg.get("title", ""),
                dataset_url=ds_url,
                license=pkg.get("license_title") or pkg.get("license_id") or "",
                resource_id=res.get("id", ""),
                resource_name=res.get("name") or "",
                format=fmt,
                size=str(res.get("size") or ""),
                themes="|".join(themes_for(res.get("name") or "",
                                           res.get("description") or "",
                                           pkg.get("title") or "")),
                kind=classify_format(fmt),
                match=match,
                url=res.get("url") or "",
            ))
    return datasets, rows


def write_inventory(out_dir: Path, datasets: Iterable[dict],
                    rows: Iterable[ResourceRow | dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    ds_path = out_dir / "datasets.jsonl"
    n_ds = 0
    with ds_path.open("w", encoding="utf-8") as fh:
        for pkg in datasets:
            fh.write(json.dumps(pkg, ensure_ascii=False) + "\n")
            n_ds += 1

    res_path = out_dir / "resources.csv"
    n_res = 0
    with res_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ResourceRow.__annotations__))
        writer.writeheader()
        for row in rows:
            writer.writerow(row if isinstance(row, dict) else row.__dict__)
            n_res += 1

    log.info("wrote %s (%d datasets), %s (%d resources)", ds_path, n_ds, res_path, n_res)
