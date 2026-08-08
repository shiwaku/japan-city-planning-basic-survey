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
import re
import sys
from pathlib import Path

import yaml

from .ckan import Catalog, CkanClient
from .fetch import fetch_all
from .convert import build_pmtiles, describe, extract_recursive, to_geojsonl
from .harvest import ResourceRow, harvest_catalog, themes_for, write_inventory
from .scrape import Site, scrape_site

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOGS = ROOT / "catalogs.yaml"
DEFAULT_SITES = ROOT / "sites.yaml"
DEFAULT_INVENTORY = ROOT / "data" / "inventory"
DEFAULT_RAW = ROOT / "data" / "raw"
DEFAULT_WORK = ROOT / "data" / "work"
DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_TILES = ROOT / "data" / "tiles"


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
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ResourceRow.__annotations__))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)
    print(f"{len(rows)} resources -> {out}")


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
        if dest.exists() and not args.force:
            manifest.append(_layer_record(rel, dest, srs, count, geom))
            continue
        if to_geojsonl(src, dest):
            logging.info("[%d/%d] %-6s %6d件 %-14s %s", n, len(sources),
                         geom[:6], count, srs or "SRS不明", rel)
            manifest.append(_layer_record(rel, dest, srs, count, geom))

    args.processed.mkdir(parents=True, exist_ok=True)
    out = args.processed / "layers.json"
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


def cmd_tiles(args: argparse.Namespace) -> None:
    """調査項目ごとに GeoJSONSeq をまとめて PMTiles にする。"""
    manifest_path = args.processed / "layers.json"
    if not manifest_path.exists():
        sys.exit(f"not found: {manifest_path}  (先に `python -m bskp convert` を実行)")
    layers = json.loads(manifest_path.read_text(encoding="utf-8"))

    groups: dict[str, list[Path]] = collections.defaultdict(list)
    for layer in layers:
        for theme in layer["themes"] or ["その他"]:
            groups[theme].append(Path(layer["path"]))

    made = []
    for theme, inputs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if args.theme and theme not in args.theme:
            continue
        slug = THEME_SLUGS.get(theme, "other")
        dest = args.tiles / f"{slug}.pmtiles"
        logging.info("%s: %d レイヤ -> %s", theme, len(inputs), dest.name)
        if build_pmtiles(inputs, dest, layer_name=slug,
                         min_zoom=args.min_zoom, max_zoom=args.max_zoom):
            made.append((theme, slug, dest, len(inputs)))

    index = [{"theme": t, "slug": s, "file": d.name, "layers": n,
              "bytes": d.stat().st_size} for t, s, d, n in made]
    args.tiles.mkdir(parents=True, exist_ok=True)
    (args.tiles / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    for t, s, d, n in made:
        print(f"  {t:<10} {d.name:<20} {d.stat().st_size / 1048576:8.1f} MiB  ({n} レイヤ)")


# 調査項目名 → ファイル名に使える slug
THEME_SLUGS = {
    "人口": "population", "産業": "industry", "土地利用": "landuse",
    "建物": "building", "都市施設": "facility", "地価": "landprice",
    "自然環境": "nature", "災害": "hazard", "景観": "landscape", "その他": "other",
}


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

    st = sub.add_parser("tiles", help="調査項目ごとにPMTilesを作る")
    st.add_argument("--processed", type=Path, default=DEFAULT_PROCESSED)
    st.add_argument("--tiles", type=Path, default=DEFAULT_TILES)
    st.add_argument("--theme", action="append", help="対象の調査項目（複数可）")
    st.add_argument("--min-zoom", type=int, default=4)
    st.add_argument("--max-zoom", type=int, default=14)
    st.set_defaults(func=cmd_tiles)

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
