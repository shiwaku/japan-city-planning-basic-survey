"""bskp — 都市計画基礎調査オープンデータの収集ツール。

    python -m bskp probe                 カタログの疎通確認
    python -m bskp harvest               横断検索してインベントリを作る
    python -m bskp report                インベントリを集計して眺める
    python -m bskp fetch --kind archive  実ファイルをダウンロード
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import logging
import os
import re
import sys
from pathlib import Path

import yaml

from . import building
from .ckan import Catalog, CkanClient
from .fetch import fetch_all
from .codetable import build_reference, normalize_code
from .convert import (build_pmtiles, dataset_srs, describe, extract_recursive,
                      is_empty_output, to_geojsonl)
from .harvest import ResourceRow, harvest_catalog, themes_for, write_inventory
from .normalize import (GROUPS, annotate, code_field, group_of, is_aggregate,
                        non_parcel_reason, strip_annotation)
from .scrape import Site, scrape_site

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOGS = ROOT / "catalogs.yaml"
DEFAULT_SITES = ROOT / "sites.yaml"
DEFAULT_INVENTORY = ROOT / "data" / "inventory"
DEFAULT_RAW = ROOT / "data" / "raw"
DEFAULT_WORK = ROOT / "data" / "work"
DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_TILES = ROOT / "data" / "tiles"
DEFAULT_REFERENCE = ROOT / "data" / "reference"


def load_catalogs(path: Path, only: list[str] | None = None,
                  include_disabled: bool = False) -> list[Catalog]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cats = [Catalog(**entry) for entry in data["catalogs"]]
    if not include_disabled:
        cats = [c for c in cats if c.enabled]
    if only:
        wanted = set(only)
        cats = [c for c in cats if c.id in wanted]
        missing = wanted - {c.id for c in cats}
        if missing:
            sys.exit(f"unknown catalog id(s): {', '.join(sorted(missing))}")
    return cats


def _setup_logging(args: argparse.Namespace) -> None:
    """画面には簡潔に、ファイルには時刻付きで全部残す。

    どのカタログをいつ走査して何件拾ったかは後から効いてくる（提供側の
    公開状況は動くので、取れなくなったときに前回との差分が知りたくなる）。
    logs/bskp.log に追記し、コマンドごとの実行結果も同じファイルに残す。
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(console)

    args.log.parent.mkdir(parents=True, exist_ok=True)
    logfile = logging.FileHandler(args.log, encoding="utf-8")
    logfile.setLevel(logging.DEBUG)
    logfile.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
    root.addHandler(logfile)


def cmd_probe(args: argparse.Namespace) -> None:
    cats = load_catalogs(args.catalogs, args.catalog, include_disabled=True)
    for cat in cats:
        try:
            total = CkanClient(cat, retries=1).ping()
            status, detail = "OK", f"{total:,} datasets"
        except Exception as exc:  # noqa: BLE001
            status, detail = "NG", str(exc)[:90]
        flag = " " if cat.enabled else "-"
        logging.info("%s %s  %-16s %-24s %s", flag, status, cat.id, detail, cat.api)


def cmd_discover(args: argparse.Namespace) -> None:
    """候補 URL のリストを叩いて、CKAN として使えるものを選り分ける。

    全国の自治体ポータルを 1 つの登録簿にまとめる API は存在しないため、
    候補を実測して catalogs.yaml に積み上げていくしかない。その測定用。
    """
    import concurrent.futures as futures

    candidates = [
        line.strip()
        for line in args.candidates.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    def check(base: str) -> tuple[str, str, str]:
        cat = Catalog(id="probe", name="probe", api=base)
        try:
            client = CkanClient(cat, retries=1, timeout=20)
            total = client.ping()
            hits = client._call("package_search", q="都市計画基礎調査", rows=0)["count"]
            return base, "OK", f"total={total:,} hits={hits:,}"
        except Exception as exc:  # noqa: BLE001
            return base, "NG", str(exc)[:70]

    with futures.ThreadPoolExecutor(args.jobs) as pool:
        for base, status, detail in pool.map(check, candidates):
            logging.info("%s  %-32s %s", status, detail, base)


def cmd_harvest(args: argparse.Namespace) -> None:
    cats = load_catalogs(args.catalogs, args.catalog)
    all_ds, all_rows = [], []
    limit = args.limit or None  # --limit 0 は「上限なし」
    for cat in cats:
        try:
            ds, rows = harvest_catalog(cat, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logging.warning("%s: harvest failed: %s", cat.id, exc)
            continue
        all_ds.extend(ds)
        all_rows.extend(rows)
    if args.catalog:
        # 一部カタログだけ走査したときに、走査しなかったカタログの結果を
        # 消してしまわないよう既存インベントリとマージする。
        kept_ds, kept_rows = _inventory_excluding(args.inventory, set(args.catalog))
        logging.info("既存インベントリから %d datasets / %d resources を引き継ぎます",
                     len(kept_ds), len(kept_rows))
        all_ds = kept_ds + all_ds
        all_rows = kept_rows + [r.__dict__ for r in all_rows]
    else:
        all_rows = [r.__dict__ for r in all_rows]

    write_inventory(args.inventory, all_ds, all_rows)
    print(f"{len(all_ds)} datasets / {len(all_rows)} resources -> {args.inventory}")


def _inventory_excluding(inventory: Path, catalog_ids: set[str]) -> tuple[list[dict], list[dict]]:
    """既存インベントリから、指定カタログ以外の行を読み出す。"""
    rows: list[dict] = []
    res_path = inventory / "resources.csv"
    if res_path.exists():
        with res_path.open(encoding="utf-8", newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if r["catalog_id"] not in catalog_ids]

    datasets: list[dict] = []
    ds_path = inventory / "datasets.jsonl"
    keep_names = {r["dataset_name"] for r in rows}
    if ds_path.exists():
        with ds_path.open(encoding="utf-8") as fh:
            datasets = [d for line in fh if line.strip()
                        if (d := json.loads(line)).get("name") in keep_names]
    return datasets, rows


def cmd_scrape(args: argparse.Namespace) -> None:
    data = yaml.safe_load(args.sites.read_text(encoding="utf-8"))
    sites = [Site(**entry) for entry in data["sites"] if entry.get("enabled", True)]
    if args.site:
        wanted = set(args.site)
        sites = [s for s in sites if s.id in wanted]

    rows: list[ResourceRow] = []
    for site in sites:
        try:
            rows.extend(scrape_site(site))
        except Exception as exc:  # noqa: BLE001 - 1 サイトの失敗で全体を止めない
            logging.warning("%s: scrape failed: %s", site.id, exc)

    out = args.inventory / "scraped.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    written = [r.__dict__ for r in rows]

    # --site で一部だけ走査したとき、走査しなかったサイトの結果を消さない。
    # harvest --catalog / tiles --theme と同じ落とし穴（部分実行が全体を上書きする）
    if args.site and out.exists():
        done = {s.id for s in sites}
        with out.open(encoding="utf-8", newline="") as fh:
            kept = [r for r in csv.DictReader(fh) if r["catalog_id"] not in done]
        logging.info("既存の scraped.csv から %d 件を引き継ぎます", len(kept))
        written = kept + written

    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ResourceRow.__annotations__))
        writer.writeheader()
        writer.writerows(written)
    print(f"{len(written)} resources -> {out}")


GEO_SUFFIXES = {".shp", ".geojson", ".json", ".gml", ".gpkg", ".kml"}


def cmd_extract(args: argparse.Namespace) -> None:
    """data/raw の書庫を data/work に展開する（入れ子ZIP・CP932対応）。"""
    archives = sorted(p for p in args.raw.rglob("*") if p.suffix.lower() == ".zip")
    logging.info("%d 個の書庫を展開します", len(archives))
    for n, archive in enumerate(archives, 1):
        rel = archive.relative_to(args.raw).with_suffix("")
        dest = args.work / rel
        if dest.exists() and not args.force:
            continue
        files = extract_recursive(archive, dest)
        logging.info("[%d/%d] %d ファイル <- %s", n, len(archives), len(files), rel)
    geo = [p for p in args.work.rglob("*") if p.suffix.lower() in GEO_SUFFIXES]
    print(f"展開完了。地物ファイル {len(geo)} 件が data/work 以下にあります")


def cmd_convert(args: argparse.Namespace) -> None:
    """data/work の地物ファイルを EPSG:4326 の GeoJSONSeq に変換する。"""
    sources = sorted(p for p in args.work.rglob("*") if p.suffix.lower() in GEO_SUFFIXES)
    if args.match:
        pat = re.compile(args.match)
        sources = [p for p in sources if pat.search(p.name)]
    logging.info("%d 件を変換します", len(sources))

    manifest: list[dict] = []
    for n, src in enumerate(sources, 1):
        rel = src.relative_to(args.work)
        dest = (args.processed / rel).with_suffix(".geojsonl")
        srs, count, geom = describe(src)
        if count == 0:
            logging.info("[%d/%d] 空のためスキップ: %s", n, len(sources), rel)
            continue
        # 空の出力は「変換済み」と見なさない。件数は変換元から数えているので、
        # 空のまま manifest に載ると欠測に気づけないまま公開してしまう
        if dest.exists() and not is_empty_output(dest) and not args.force:
            manifest.append(_layer_record(rel, dest, srs, count, geom))
            continue
        # 座標系が読めないと ogr2ogr は終了コード 0 のまま空を書く。.prj が無い
        # だけなら同じデータセットの他レイヤから借りる
        source_srs = dataset_srs(src, args.work) if not srs else ""
        if to_geojsonl(src, dest, source_srs):
            if is_empty_output(dest):
                logging.warning("[%d/%d] 変換結果が空です（%d件のはず）: %s",
                                n, len(sources), count, rel)
                continue
            logging.info("[%d/%d] %-6s %6d件 %-14s %s", n, len(sources),
                         geom[:6], count, srs or source_srs or "SRS不明", rel)
            manifest.append(_layer_record(rel, dest, srs or source_srs, count, geom))

    args.processed.mkdir(parents=True, exist_ok=True)
    out = args.processed / "layers.json"

    # --match で一部だけ変換したとき、対象外のレイヤを manifest から消さない
    # （scrape --site / tiles --theme と同じ落とし穴）。今回試した変換元は
    # 結果がどうであれ入れ替える。失敗したものの古い記録を残さないため。
    if args.match and out.exists():
        tried = {p.relative_to(args.work).as_posix() for p in sources}
        kept = [m for m in json.loads(out.read_text(encoding="utf-8"))
                if m["source"] not in tried]
        logging.info("既存 layers.json から %d レイヤを引き継ぎます", len(kept))
        manifest = kept + manifest

    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(m["features"] for m in manifest)
    print(f"{len(manifest)} レイヤ / {total:,} フィーチャ -> {args.processed}")


def _layer_record(rel: Path, dest: Path, srs: str, count: int, geom: str) -> dict:
    parts = rel.parts
    return {
        "path": dest.as_posix(),
        "source": rel.as_posix(),
        "catalog_id": parts[0] if parts else "",
        "dataset_name": parts[1] if len(parts) > 1 else "",
        "layer": rel.stem,
        "srs": srs,
        "features": count,
        "geometry": geom,
        "themes": themes_for(rel.stem, *parts),
    }


def cmd_normalize(args: argparse.Namespace) -> None:
    """変換済みの GeoJSONSeq に、全国共通の用途コードを足す。

    土地利用は lui_code / lui_name、建物は bui_code / bui_name（＋実測の高さ）。
    convert が作った *.geojsonl を読み書きするだけなので、再変換は要らない。
    """
    if args.target in ("landuse", "all"):
        _normalize_landuse(args)
    if args.target in ("building", "all"):
        _normalize_buildings(args)


def _normalize_landuse(args: argparse.Namespace) -> None:
    """土地利用に lui_code / lui_name を足す。"""
    reference = json.loads(
        (args.reference / "landuse_codes.json").read_text(encoding="utf-8"))
    index = _pref_index(args.inventory, reference)

    targets = [p for p in args.processed.rglob("*.geojsonl")
               if re.search(r"土地利用|tochiriyou|landuse", p.name, re.I)]
    logging.info("%d レイヤを正規化します", len(targets))

    stats: collections.Counter = collections.Counter()
    skipped_aggregate = 0
    for n, path in enumerate(targets, 1):
        parts = path.relative_to(args.processed).parts
        pref = args.pref or index.get(parts[1] if len(parts) > 1 else "", "")
        lines_out = []
        field: str | None = None
        is_national = False
        resolved = False
        aggregate = False
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            feature = json.loads(raw)
            props = feature.get("properties") or {}
            if not resolved:
                resolved = True
                if is_aggregate(list(props)):
                    # 小地域集計型は敷地ベースの土地利用とは別物なので正規化しない
                    skipped_aggregate += 1
                    aggregate = True
                    break
                field, is_national = code_field(list(props))
            feature["properties"] = annotate(props, pref, reference, field, is_national)
            # 系統は集計表示のためだけに数える。属性としては書かない
            stats[group_of(feature["properties"]["lui_code"])] += 1
            lines_out.append(json.dumps(feature, ensure_ascii=False))
        if aggregate:
            # 集計型と判定する前の実行が注釈を書き込んでいることがある。
            # 残しておくと「小地域まるごとに用途1つ」の嘘になるので消す
            _strip_annotation(path)
            continue
        if not lines_out:
            continue
        path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
        if n % 100 == 0 or n == len(targets):
            logging.info("[%d/%d] %s", n, len(targets), parts[1] if len(parts) > 1 else "")

    total = sum(stats.values())
    print(f"{len(targets) - skipped_aggregate} レイヤ / {total:,} フィーチャ"
          f"（小地域集計型 {skipped_aggregate} レイヤは対象外）")
    for group in GROUPS:
        count = stats.get(group, 0)
        if total:
            print(f"  {group:<10} {count:>9,}  {count / total * 100:5.1f}%")


def _normalize_buildings(args: argparse.Namespace) -> None:
    """建物に bui_code / bui_name と、実測があれば bui_height / bui_floors を足す。

    対照表を持つのは用途コード列がある publisher だけ（東京都 BV_6・さいたま市
    RIYOU）。無いレイヤは触らない。建物フットプリントとしては正しくても、
    用途を持たないものに用途を作ることはできない。
    """
    targets = [p for p in args.processed.rglob("*.geojsonl")
               if re.search(r"建物|建築|tatemono|house", p.name, re.I)]
    logging.info("建物 %d レイヤを見ます", len(targets))

    stats: collections.Counter = collections.Counter()
    heights = 0
    dropped = 0
    skipped: collections.Counter = collections.Counter()
    done = 0
    for n, path in enumerate(targets, 1):
        lines_out = []
        field: str | None = None
        resolved = False
        skip = ""
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            feature = json.loads(raw)
            props = feature.get("properties") or {}
            if not resolved:
                resolved = True
                if is_aggregate(list(props)):
                    skip = "小地域集計型"
                    break
                field = building.code_field(list(props))
                if field is None:
                    skip = "用途コード列なし"
                    break
            if building.is_not_a_building(props, field):
                # さいたま市 RIYOU=88「建物としてカウントしない構造物等」。
                # 写せなかったのではなく、提供元が建物ではないと言っている
                dropped += 1
                continue
            feature["properties"] = building.annotate(props, field)
            stats[feature["properties"]["bui_name"] or "未分類"] += 1
            if "bui_height" in feature["properties"]:
                heights += 1
            lines_out.append(json.dumps(feature, ensure_ascii=False))
        if skip:
            skipped[skip] += 1
            _strip_annotation(path, building.strip_annotation)
            continue
        if not lines_out:
            continue
        path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
        done += 1
        logging.info("[%d/%d] %s %d棟", n, len(targets), path.name, len(lines_out))

    total = sum(stats.values())
    print(f"建物 {done} レイヤ / {total:,} 棟に用途コードを付けました"
          f"（実測の高さあり {heights:,} 棟 / "
          f"{'  '.join(f'{k} {v}レイヤ' for k, v in skipped.most_common())}）")
    if dropped:
        print(f"  建物としてカウントしない構造物 {dropped:,} 件は出力しない")
    for name, count in stats.most_common():
        print(f"  {name:<12} {count:>9,}  {count / total * 100:5.1f}%")


def _strip_annotation(path: Path, strip=strip_annotation) -> bool:
    """正規化の注釈（lui_code など）が付いていたら取り除く。消したら True。"""
    lines = []
    changed = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        feature = json.loads(raw)
        props = feature.get("properties") or {}
        stripped = strip(props)
        if len(stripped) != len(props):
            changed = True
            feature["properties"] = stripped
        lines.append(json.dumps(feature, ensure_ascii=False))
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logging.info("集計型に残っていた注釈を削除: %s", path.name)
    return changed


def _first_properties(path: Path) -> dict | None:
    """先頭フィーチャの属性を返す。空ファイルなら None。"""
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                if raw.strip():
                    return json.loads(raw).get("properties") or {}
    except (OSError, json.JSONDecodeError):
        return None
    return None


LANDUSE_THEME = "土地利用"
BUILDING_THEME = "建物"

# 調査項目の定義どおりの形をしているレイヤだけタイルに入れる。
# 土地利用現況は「1ポリゴン=1敷地に用途」、建物利用現況は「1ポリゴン=1棟に用途」。
# 調査項目名での振り分けには、同じ名前で別物（小地域集計・区域界・施設の点）が
# 入ってくるので、データ自身の形と属性で判定する
DEFINED_SHAPE = {
    LANDUSE_THEME: non_parcel_reason,
    BUILDING_THEME: building.non_building_reason,
}

# 配信タイルに載せる調査項目。収集と変換は 10 項目ぶん続けるが、タイルにするのは
# この 2 つだけにする。
#
# 外した 8 項目（人口・産業・都市施設・地価・自然環境・災害・景観・その他）は、
# どれも国が全国整備している一次データの劣化版になっていた:
#
#   人口・産業   250m/500m メッシュと小地域の集計値。国勢調査・経済センサスの
#                同じ集計が e-Stat 統計 GIS に全国・全年次ぶんある
#   地価         地価公示・都道府県地価調査は国土数値情報に全国・毎年更新
#   災害         浸水想定区域・土砂災害・避難場所も国土数値情報
#   景観         文化財も同上
#   都市施設     公園・道路も同上
#   その他       用途地域・都市計画区域・行政区域も同上
#
# こちらは自治体ごとに虫食いで、年次もばらばらで、正規化もしていない。
# 対して土地利用と建物は国の全国整備がなく、自治体のオープンデータを横断して
# 国標準コードに正規化したこのタイル自体に意味がある。
PUBLISHED_THEMES = (LANDUSE_THEME, BUILDING_THEME)


def _drop_reason(theme: str, layer: dict) -> str | None:
    """そのテーマから外す理由。定義どおりの形なら None。"""
    check = DEFINED_SHAPE.get(theme)
    if check is None:
        return None
    props = _first_properties(Path(layer["path"]))
    if props is None:
        return "空ファイル"
    return check(layer["geometry"], list(props))


def cmd_tiles(args: argparse.Namespace) -> None:
    """PUBLISHED_THEMES の GeoJSONSeq をまとめて PMTiles にする。"""
    manifest_path = args.processed / "layers.json"
    if not manifest_path.exists():
        sys.exit(f"not found: {manifest_path}  (先に `python -m bskp convert` を実行)")
    layers = json.loads(manifest_path.read_text(encoding="utf-8"))

    # ライセンス不明のデータセットは配布タイルに入れない。
    # convert は layers.json を作り直すので、手で消しても復活する。
    # 除外は licenses.yaml に持たせて毎回効くようにする。
    lic_path = args.reference / "licenses.yaml"
    if lic_path.exists():
        excluded = {e["dataset"] for e in
                    (yaml.safe_load(lic_path.read_text(encoding="utf-8")) or {}).get("exclude") or []}
        before = len(layers)
        layers = [l for l in layers if l["dataset_name"] not in excluded]
        if before != len(layers):
            logging.info("ライセンス不明のため %d レイヤを除外しました", before - len(layers))

    groups: dict[str, list[Path]] = collections.defaultdict(list)
    tiled: list[dict] = []
    dropped: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    out_of_scope = 0
    for layer in layers:
        in_scope = [t for t in (layer["themes"] or []) if t in PUBLISHED_THEMES]
        if not in_scope:
            out_of_scope += 1
            continue
        themes = []
        for theme in in_scope:
            if reason := _drop_reason(theme, layer):
                dropped[theme][reason] += 1
                continue
            themes.append(theme)
        if not themes:
            continue
        for theme in themes:
            groups[theme].append(Path(layer["path"]))
        tiled.append(layer)
    if out_of_scope:
        logging.info("配信対象の調査項目でない %d レイヤを除外しました"
                     "（収集と変換はしているので data/processed には残る）", out_of_scope)
    for theme, reasons in dropped.items():
        logging.info("%s から定義どおりでない %d レイヤを除外しました（%s）",
                     theme, sum(reasons.values()),
                     " / ".join(f"{k} {v}" for k, v in reasons.most_common()))

    made = []
    for theme, inputs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if args.theme and theme not in args.theme:
            continue
        slug = THEME_SLUGS[theme]
        dest = args.tiles / f"{slug}.pmtiles"
        logging.info("%s: %d レイヤ -> %s", theme, len(inputs), dest.name)
        if build_pmtiles(inputs, dest, layer_name=slug,
                         min_zoom=args.min_zoom, max_zoom=args.max_zoom):
            made.append((theme, slug, dest, len(inputs)))

    index = [{"theme": t, "slug": s, "file": d.name, "layers": n,
              "bytes": d.stat().st_size} for t, s, d, n in made]
    args.tiles.mkdir(parents=True, exist_ok=True)

    # --theme で一部だけ作り直したとき、作らなかったテーマを index から
    # 消してしまわないよう既存分とマージする（ビューアが読めなくなる）
    index_path = args.tiles / "index.json"
    if args.theme and index_path.exists():
        rebuilt = {e["slug"] for e in index}
        kept = [e for e in json.loads(index_path.read_text(encoding="utf-8"))
                if e["slug"] not in rebuilt and (args.tiles / e["file"]).exists()]
        logging.info("既存 index から %d テーマを引き継ぎます", len(kept))
        index = kept + index
    (args.tiles / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    # 出典表示。CC-BY も GNU FDL も再配布には表示が要るので、
    # タイルの元になったデータセットから実際の提供元とライセンスを拾って出す。
    # 手書きにすると変換対象が変わったときに嘘になる。
    attribution = _attribution_for(tiled, args.inventory)
    (args.tiles / "attribution.json").write_text(
        json.dumps(attribution, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("出典 %d 件を attribution.json に書き出しました", len(attribution))

    # 対象自治体。全国を配れているように見せない（実際は数都県ぶんしかない）ため、
    # どこが入っているかをビューアが出せる形にしておく。
    areas = _areas_for(tiled, args.inventory, args.reference, args.work)
    (args.tiles / "areas.json").write_text(
        json.dumps(areas, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("対象地域 %d 件（提供元 %d）を areas.json に書き出しました",
                 sum(len(g["areas"]) for g in areas), len(areas))

    for t, s, d, n in made:
        print(f"  {t:<10} {d.name:<20} {d.stat().st_size / 1048576:8.1f} MiB  ({n} レイヤ)")


def _attribution_for(layers: list[dict], inventory: Path,
                     reference: Path | None = None) -> list[dict]:
    """タイルに含まれるデータの提供元とライセンスを、インベントリから引き当てる。

    カタログのメタデータが「その他」や空のことがあるので、一次情報で確認した内容を
    licenses.yaml で補う。再配布するのに条件不明のまま出さないため。
    """
    verified: dict[str, dict] = {}
    ref_dir = reference or (inventory.parent / "reference")
    lic_path = ref_dir / "licenses.yaml"
    if lic_path.exists():
        verified = (yaml.safe_load(lic_path.read_text(encoding="utf-8")) or {}).get("overrides") or {}
    used = {(l["catalog_id"], l["dataset_name"]) for l in layers}
    seen: dict[tuple[str, str], dict] = {}
    for row in _read_inventory(inventory):
        key = (row["catalog_id"], row["dataset_name"])
        if key not in used or key in seen:
            continue
        fixed = verified.get(row["organization"])
        seen[key] = {
            "organization": row["organization"],
            "license": fixed["license"] if fixed else row["license"],
            "catalog": row["catalog_name"],
            "url": row["dataset_url"],
        }

    # 組織＋ライセンス単位にまとめる。同じ県のデータセットが何十件も並んでも意味がない
    grouped: dict[tuple[str, str], dict] = {}
    for rec in seen.values():
        key = (rec["organization"], rec["license"])
        entry = grouped.setdefault(key, {
            "organization": rec["organization"],
            "license": rec["license"],
            "catalog": rec["catalog"],
            "datasets": 0,
            "url": rec["url"],
        })
        entry["datasets"] += 1
    return sorted(grouped.values(), key=lambda e: -e["datasets"])


def _shp_bbox(path: Path) -> tuple[float, float, float, float] | None:
    """シェープファイルのヘッダに入っている外接矩形。

    先頭 100 バイトを読むだけで済む。変換後の GeoJSONSeq を走査すると
    東京都の建物だけで 1.2 GiB あり、範囲を出すためだけに払うには重い。
    """
    import struct
    try:
        with path.open("rb") as f:
            head = f.read(100)
    except OSError:
        return None
    if len(head) < 100 or head[:4] != b"\x00\x00\x27\x0a":  # ファイルコード 9994
        return None
    xmin, ymin, xmax, ymax = struct.unpack("<4d", head[36:68])
    if not (xmin <= xmax and ymin <= ymax):
        return None
    return xmin, ymin, xmax, ymax


def _to_wgs84(srs: str, points: list[tuple[float, float]]) -> list[tuple[float, float]] | None:
    """gdaltransform に一括で通す。SRS ごとに 1 プロセスで済ませる。"""
    import subprocess
    if not points:
        return []
    stdin = "".join(f"{x} {y}\n" for x, y in points)
    try:
        done = subprocess.run(["gdaltransform", "-s_srs", srs, "-t_srs", "EPSG:4326"],
                              input=stdin, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    moved = [(float(p[0]), float(p[1])) for line in done.stdout.splitlines()
             if len(p := line.split()) >= 2]
    return moved if len(moved) == len(points) else None


def _bboxes(layers: list[dict], work: Path) -> dict[str, list[float]]:
    """レイヤの path → EPSG:4326 の [w, s, e, n]。

    投影が違えば矩形は変換後に傾くので、4 隅を通してから min/max を取る。
    """
    per_srs: dict[str, list[tuple[str, tuple]]] = collections.defaultdict(list)
    for layer in layers:
        if box := _shp_bbox(work / layer["source"]):
            per_srs[layer["srs"] or "EPSG:4326"].append((layer["path"], box))

    out: dict[str, list[float]] = {}
    for srs, items in per_srs.items():
        corners = [pt for _, (xmin, ymin, xmax, ymax) in items
                   for pt in ((xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax))]
        moved = _to_wgs84(srs, corners)
        if not moved:
            logging.warning("%s の座標変換に失敗しました。%d レイヤの範囲を出せません",
                            srs, len(items))
            continue
        for i, (path, _) in enumerate(items):
            quad = moved[i * 4:i * 4 + 4]
            lon = [p[0] for p in quad]
            lat = [p[1] for p in quad]
            out[path] = [min(lon), min(lat), max(lon), max(lat)]
    return out


def _area_name(layer: dict, rules: dict) -> str:
    """レイヤ名から対象自治体を拾う。拾えなければレイヤ名をそのまま返す。"""
    ds = (rules.get("datasets") or {}).get(layer["dataset_name"]) or {}
    if name := (ds.get("layers") or {}).get(layer["layer"]):
        return name
    if name := ds.get("name"):
        return name
    for pattern in rules.get("patterns") or []:
        if m := re.match(pattern, layer["layer"]):
            return m.group(1)
    return layer["layer"]


def _areas_for(layers: list[dict], inventory: Path, reference: Path,
               work: Path) -> list[dict]:
    """タイルに入った範囲を、提供元 → 対象自治体の形にまとめる。

    全国の基礎調査を配れているように見えてしまうのを防ぐためのもの。実際は
    東京・埼玉・静岡市・津島市しか入っていない。名前の付け方は自治体ごとに
    ばらばらなので、拾う規則は areas.yaml に持たせる。
    """
    rules = {}
    rule_path = reference / "areas.yaml"
    if rule_path.exists():
        rules = yaml.safe_load(rule_path.read_text(encoding="utf-8")) or {}

    provider: dict[tuple[str, str], tuple[str, str, str]] = {}
    for row in _read_inventory(inventory):
        provider.setdefault((row["catalog_id"], row["dataset_name"]),
                            (row["organization"], row["catalog_name"], row["dataset_url"]))

    boxes = _bboxes(layers, work)
    groups: dict[str, dict] = {}
    for layer in layers:
        who = provider.get((layer["catalog_id"], layer["dataset_name"]))
        if not who:
            continue
        organization, catalog, url = who
        group = groups.setdefault(organization, {
            "provider": organization, "catalog": catalog, "url": url, "areas": {}})
        name = _area_name(layer, rules)
        area = group["areas"].setdefault(name, {
            "name": name, "themes": set(), "features": 0, "bbox": None})
        area["themes"].update(t for t in (layer["themes"] or [])
                              if t in PUBLISHED_THEMES and not _drop_reason(t, layer))
        area["features"] += layer.get("features") or 0
        if box := boxes.get(layer["path"]):
            area["bbox"] = box if not area["bbox"] else [
                min(area["bbox"][0], box[0]), min(area["bbox"][1], box[1]),
                max(area["bbox"][2], box[2]), max(area["bbox"][3], box[3])]

    out = []
    for group in groups.values():
        areas = sorted(group["areas"].values(), key=lambda a: -a["features"])
        for area in areas:
            area["themes"] = [t for t in PUBLISHED_THEMES if t in area["themes"]]
        out.append({**group, "areas": areas,
                    "features": sum(a["features"] for a in areas)})
    return sorted(out, key=lambda g: -g["features"])


# 調査項目名 → ファイル名に使える slug。PUBLISHED_THEMES と対で持つ
THEME_SLUGS = {LANDUSE_THEME: "landuse", BUILDING_THEME: "building"}


def cmd_codetable(args: argparse.Namespace) -> None:
    """国交省の対照表・コード表を解析して landuse_codes.json を作る。"""
    ref = build_reference(args.reference / "mlit_landuse_crosswalk.xlsx",
                          args.reference / "mlit_code_table.xlsx")
    out = args.reference / "landuse_codes.json"
    out.write_text(json.dumps(ref, ensure_ascii=False, indent=2), encoding="utf-8")
    with_codes = sum(1 for v in ref["prefectures"].values() if v["to_national"])
    total = sum(len(v["to_national"]) for v in ref["prefectures"].values())
    print(f"国標準コード {len(ref['national_codes'])} 区分 / "
          f"{len(ref['prefectures'])}都道府県（独自コードあり {with_codes}県）/ 対応 {total} 件 -> {out}")


def cmd_coverage(args: argparse.Namespace) -> None:
    """土地利用レイヤの用途コードが、対照表でどれだけ写せるかを実測する。

    対照表は公式だが実データを完全にはカバーしない（さいたま市の 141-144・150 は
    埼玉県のシートに載っていない）。どこが写せないかを黙って捨てないための計測。
    """
    import subprocess

    ref = json.loads((args.reference / "landuse_codes.json").read_text(encoding="utf-8"))
    env = {**os.environ, "SHAPE_ENCODING": "CP932"}
    field_re = re.compile(r"^\s+(LANDUSE|landuse|lu_code|youto)\s*\(\w+\)\s*=\s*(.+)$")

    targets = [p for p in args.work.rglob("*.shp")
               if re.search(r"土地利用|tochiriyou|landuse", p.name, re.I)]
    index = _pref_index(args.inventory, ref)

    grand = collections.Counter()
    unknown: set[str] = set()
    national_style = 0
    for path in targets[: args.limit]:
        parts = path.relative_to(args.work).parts
        dataset = parts[1] if len(parts) > 1 else ""
        pref = args.pref or index.get(dataset, "")
        out = subprocess.run(["ogrinfo", "-al", "-geom=NO", "-fields=YES", str(path)],
                             capture_output=True, env=env).stdout.decode("utf-8", "replace")
        vals = collections.Counter()
        for line in out.splitlines():
            m = field_re.match(line)
            if m:
                vals[m.group(2).strip()] += 1
        if not vals:
            # lui_201… 形式（国標準の列名で面積が入る型）。用途コード列を持たないので
            # 写像は不要。件数だけ数えておく
            national_style += 1
            continue
        if not pref:
            unknown.add(dataset)
            continue
        ok = sum(n for v, n in vals.items() if normalize_code(ref, pref, v))
        tot = sum(vals.values())
        grand["ok"] += ok
        grand["total"] += tot
        miss = {v: n for v, n in vals.items() if not normalize_code(ref, pref, v)}
        status = "OK " if not miss else "欠 "
        logging.info("%s %5.1f%% %7d件 %-8s %s", status, ok / tot * 100, tot, pref,
                     path.relative_to(args.work))
        for v, n in sorted(miss.items(), key=lambda kv: -kv[1])[:6]:
            logging.info("      未対応 %-6s %6d件", v, n)

    print(f"\n区分図型 {grand['total']:,} フィーチャ中 {grand['ok']:,} "
          f"({grand['ok'] / grand['total'] * 100:.1f}%) が国標準コードに写せます"
          if grand["total"] else "\n区分図型のレイヤはありませんでした")
    print(f"国標準列型（lui_*）のレイヤ: {national_style} 件（写像不要）")
    if unknown:
        print(f"都道府県を特定できないデータセット: {len(unknown)} 件 "
              f"({', '.join(sorted(unknown)[:4])}…)")


def _pref_index(inventory: Path, ref: dict) -> dict[str, str]:
    """データセット名 -> 都道府県名。インベントリの提供組織から引く。

    パス中の数字から推測してはいけない。岐阜県のデータセット名は `c11654-116` で、
    先頭2桁を JIS コードとして読むと埼玉県(11)になる。組織名が唯一の確かな出所。
    """
    names = sorted(ref["prefectures"], key=len, reverse=True)
    index: dict[str, str] = {}
    for row in _read_inventory(inventory):
        org = row.get("organization") or ""
        title = row.get("dataset_title") or ""
        for pref in names:
            if pref in org or pref in title:
                index[row["dataset_name"]] = pref
                break
        else:
            # 「都市・まちづくり推進課」のように県名を含まない組織名があるので
            # カタログ名・カタログIDからも引く
            for key, pref in (("oita", "大分県"), ("gifu", "岐阜県"),
                              ("shizuoka", "静岡県"), ("kanagawa", "神奈川県"),
                              ("yamaguchi", "山口県"), ("tsushima", "愛知県"),
                              ("saitama", "埼玉県"), ("tokyo", "東京都")):
                if key in row["catalog_id"] or key in row["dataset_name"]:
                    index[row["dataset_name"]] = pref
                    break
    return index


def _read_inventory(inventory: Path) -> list[dict]:
    """resources.csv と scraped.csv を両方読む（あるものだけ）。"""
    rows: list[dict] = []
    for name in ("resources.csv", "scraped.csv"):
        path = inventory / name
        if path.exists():
            with path.open(encoding="utf-8", newline="") as fh:
                rows.extend(csv.DictReader(fh))
    return rows


def cmd_report(args: argparse.Namespace) -> None:
    rows = _read_inventory(args.inventory)
    if not rows:
        sys.exit(f"not found: {args.inventory}/resources.csv  "
                 "(先に `python -m bskp harvest` を実行)")

    datasets = {(r["catalog_id"], r["dataset_name"]) for r in rows}
    known = sum(int(r["size"]) for r in rows if r["size"].isdigit())
    print(f"datasets: {len(datasets)}   resources: {len(rows)}   "
          f"known size: {known / 1073741824:.2f} GiB "
          f"({sum(1 for r in rows if not r['size'].isdigit())} 件はサイズ不明)\n")

    def tally(title: str, key, top: int | None = None) -> None:
        counter = collections.Counter(key(r) for r in rows)
        print(f"--- {title} ---")
        for name, count in counter.most_common(top):
            print(f"  {count:5d}  {name or '(なし)'}")
        print()

    tally("カタログ別", lambda r: f"{r['catalog_id']} ({r['catalog_name']})")
    tally("提供組織別 上位20", lambda r: r["organization"], top=20)
    tally("形式別", lambda r: r["format"])
    tally("種別", lambda r: r["kind"])
    tally("一致経路", lambda r: r.get("match", ""))

    theme_counter: collections.Counter = collections.Counter()
    for r in rows:
        for t in filter(None, r["themes"].split("|")):
            theme_counter[t] += 1
    print("--- 調査項目別（リソース名からの推定・重複あり） ---")
    for name, count in theme_counter.most_common():
        print(f"  {count:5d}  {name}")


def cmd_fetch(args: argparse.Namespace) -> None:
    cats = load_catalogs(args.catalogs, None, include_disabled=True)
    fetch_all(
        _read_inventory(args.inventory),
        args.raw,
        kinds=set(args.kind) if args.kind else None,
        max_bytes=args.max_mb * 1048576 if args.max_mb else None,
        dry_run=args.dry_run,
        limit=args.limit,
        user_agents={c.id: c.user_agent for c in cats if c.user_agent},
    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="bskp", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--catalogs", type=Path, default=DEFAULT_CATALOGS)
    p.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    p.add_argument("--log", type=Path, default=ROOT / "logs" / "bskp.log",
                   help="走査ログの追記先")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("probe", help="カタログの疎通確認")
    sp.add_argument("--catalog", action="append", help="対象カタログ id（複数可）")
    sp.set_defaults(func=cmd_probe)

    sd = sub.add_parser("discover", help="候補URLがCKANとして使えるか実測する")
    sd.add_argument("candidates", type=Path, help="1行1URLの候補リスト")
    sd.add_argument("--jobs", type=int, default=8)
    sd.set_defaults(func=cmd_discover)

    sh = sub.add_parser("harvest", help="横断検索してインベントリを作る")
    sh.add_argument("--catalog", action="append", help="対象カタログ id（複数可）")
    sh.add_argument("--limit", type=int, default=2000,
                    help="カタログ・クエリあたりの取得上限。0 で全件走査（既定 2000）")
    sh.set_defaults(func=cmd_harvest)

    sc = sub.add_parser("scrape", help="CKAN以外の配布サイトからリンクを拾う")
    sc.add_argument("--sites", type=Path, default=DEFAULT_SITES)
    sc.add_argument("--site", action="append", help="対象サイト id（複数可）")
    sc.set_defaults(func=cmd_scrape)

    se = sub.add_parser("extract", help="書庫を展開する（入れ子ZIP・CP932対応）")
    se.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    se.add_argument("--work", type=Path, default=DEFAULT_WORK)
    se.add_argument("--force", action="store_true")
    se.set_defaults(func=cmd_extract)

    sv = sub.add_parser("convert", help="地物ファイルをEPSG:4326のGeoJSONSeqに変換")
    sv.add_argument("--work", type=Path, default=DEFAULT_WORK)
    sv.add_argument("--processed", type=Path, default=DEFAULT_PROCESSED)
    sv.add_argument("--match", help="ファイル名の絞り込み正規表現")
    sv.add_argument("--force", action="store_true")
    sv.set_defaults(func=cmd_convert)

    st = sub.add_parser("tiles", help="土地利用・建物のPMTilesを作る")
    st.add_argument("--processed", type=Path, default=DEFAULT_PROCESSED)
    st.add_argument("--tiles", type=Path, default=DEFAULT_TILES)
    st.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    # areas.json の範囲はシェープファイルのヘッダから作るので data/work が要る
    st.add_argument("--work", type=Path, default=DEFAULT_WORK)
    st.add_argument("--theme", action="append", choices=PUBLISHED_THEMES,
                    help="対象の調査項目（複数可。既定は両方）")
    st.add_argument("--min-zoom", type=int, default=4)
    st.add_argument("--max-zoom", type=int, default=14)
    st.set_defaults(func=cmd_tiles)

    sct = sub.add_parser("codetable", help="国交省の対照表を解析してコード辞書を作る")
    sct.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    sct.set_defaults(func=cmd_codetable)

    scv = sub.add_parser("coverage", help="用途コードが対照表でどれだけ写せるか実測する")
    scv.add_argument("--work", type=Path, default=DEFAULT_WORK)
    scv.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    scv.add_argument("--pref", help="都道府県名を明示する（推定に任せない場合）")
    scv.add_argument("--limit", type=int, default=40)
    scv.set_defaults(func=cmd_coverage)

    sn = sub.add_parser("normalize", help="土地利用・建物に全国共通の用途コードを付ける")
    sn.add_argument("--processed", type=Path, default=DEFAULT_PROCESSED)
    sn.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    sn.add_argument("--pref", help="都道府県名を明示する")
    sn.add_argument("--target", choices=("all", "landuse", "building"), default="all",
                    help="対象の調査項目（既定は両方）")
    sn.set_defaults(func=cmd_normalize)

    sr = sub.add_parser("report", help="インベントリを集計")
    sr.set_defaults(func=cmd_report)

    sf = sub.add_parser("fetch", help="実ファイルをダウンロード")
    sf.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    sf.add_argument("--kind", action="append",
                    choices=["geo", "archive", "tabular", "document", "other"],
                    help="取得する種別（既定は全部）")
    sf.add_argument("--max-mb", type=int, help="この MB を超えるリソースは飛ばす")
    sf.add_argument("--limit", type=int, help="取得件数の上限")
    sf.add_argument("--dry-run", action="store_true", help="URL と保存先を出すだけ")
    sf.set_defaults(func=cmd_fetch)

    args = p.parse_args(argv)
    _setup_logging(args)
    logging.info("$ bskp %s", " ".join(argv if argv is not None else sys.argv[1:]))
    try:
        args.func(args)
    finally:
        logging.info("--- end of %s ---", args.command)


if __name__ == "__main__":
    main()
