"""取得した生ファイルを、Web 地図に載せられる形（GeoJSON / PMTiles）に変換する。

生データ側の事情:
  - ZIP が入れ子（静岡県は ZIP の中に ZIP がもう一段）
  - ZIP エントリ名も DBF も CP932。Python の zipfile は cp437 として読む
  - 座標系が平面直角座標系でバラバラ（系番号は県ごと。日田市は EPSG:2444）
  - リソースの format 表記が実体と食い違う（「shpデータ」の中身が CSV だけ、など）

そこで「展開してから中身で判定する」方針をとる。format は当てにしない。

    data/raw/**.zip  --extract-->  data/work/<catalog>/<dataset>/**
                     --detect--->  *.shp を探す
                     --ogr2ogr-->  data/processed/**.geojsonl（EPSG:4326）
                     --tippecanoe-> data/tiles/*.pmtiles
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# ZIP エントリ名の文字化け対策。zipfile は flag bit 0x800 が立っていない名前を
# cp437 として読むので、日本語名は cp932 に戻す必要がある
_ZIP_UTF8_FLAG = 0x800


def zip_entry_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & _ZIP_UTF8_FLAG:
        return info.filename
    for enc in ("cp932", "utf-8"):
        try:
            return info.filename.encode("cp437").decode(enc)
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return info.filename


_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f]')


def _safe_relpath(name: str) -> Path:
    """ZIP 内パスを安全な相対パスにする。.. や絶対パスは弾く（Zip Slip 対策）。"""
    parts = []
    for part in name.replace("\\", "/").split("/"):
        part = _UNSAFE.sub("_", part).strip()
        if part in ("", ".", ".."):
            continue
        parts.append(part)
    return Path(*parts) if parts else Path("_")


def extract_recursive(archive: Path, dest: Path, depth: int = 0,
                      max_depth: int = 3) -> list[Path]:
    """ZIP を再帰的に展開する。入れ子 ZIP は中の ZIP も開く。"""
    if depth > max_depth:
        log.warning("入れ子が深すぎるので打ち切り: %s", archive)
        return []
    written: list[Path] = []
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        log.warning("ZIP として開けません: %s", archive)
        return []
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = _safe_relpath(zip_entry_name(info))
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            written.append(out)

    nested = [p for p in written if p.suffix.lower() == ".zip"]
    for inner in nested:
        written += extract_recursive(inner, inner.with_suffix(""), depth + 1, max_depth)
    return written


@dataclass
class Layer:
    """変換対象の 1 レイヤ（= 1 つの .shp / .geojson）。"""
    source: Path
    name: str
    catalog_id: str
    dataset_name: str
    srs: str = ""
    features: int = 0
    geometry: str = ""
    themes: list[str] = field(default_factory=list)


def _ogrinfo(path: Path, encoding: str = "CP932") -> dict:
    """ogrinfo -so -al -json でレイヤの素性を読む。

    SHAPE_ENCODING を渡しても、DBF 由来のフィールド名がそのまま CP932 バイトで
    出てくることがあり、json.loads がそこで落ちる。素性（SRS・件数・型）が
    取れれば十分なので、デコードできないバイトは置換して読み進める。
    """
    try:
        out = subprocess.run(
            ["ogrinfo", "-so", "-al", "-json", str(path)],
            capture_output=True, timeout=180, check=True,
            env={**_env(), "SHAPE_ENCODING": encoding},
        ).stdout
        return json.loads(out.decode("utf-8", "replace"))
    except subprocess.CalledProcessError as exc:
        log.warning("ogrinfo 失敗 %s: %s", path, exc.stderr.decode("utf-8", "replace")[:200])
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        log.warning("ogrinfo 失敗 %s: %s", path, exc)
    return {}


def describe(path: Path) -> tuple[str, int, str]:
    """(SRS識別子, フィーチャ数, ジオメトリ型) を返す。読めなければ空。"""
    meta = _ogrinfo(path)
    layers = meta.get("layers") or []
    if not layers:
        return "", 0, ""
    layer = layers[0]
    srs = ""
    ref = (layer.get("geometryFields") or [{}])[0].get("coordinateSystem") or {}
    proj = ref.get("projjson") or {}
    ident = proj.get("id") or {}
    if ident.get("authority") and ident.get("code"):
        srs = f"{ident['authority']}:{ident['code']}"
    elif proj.get("name"):
        srs = proj["name"]
    return srs, int(layer.get("featureCount") or 0), layer.get("geometryFields", [{}])[0].get("type", "")


# 文字化けの目印。UTF-8 のバイト列を CP932 として読むと、この帯の文字が大量に出る
# （'佐藤' が '菴占陸' のようになる）。逆方向の化けは別の帯になる
_MOJIBAKE = re.compile(r"[繧繝縺蜿蜷菴閭髢郢輔ｼｦｧｨｩｪ]")


def detect_encoding(source: Path) -> str:
    """DBF の文字コードを決める。CP932 決め打ちだと UTF-8 のものが化ける。

    自治体データは CP932 が多数だが UTF-8 も混ざる（h29luss は UTF-8 で、
    CP932 を強制すると属性名が '菴乗園繧ｳ' になった）。
    属性名を両方で読んでみて、化けの少ないほうを採る。
    """
    for encoding in ("CP932", "UTF-8"):
        meta = _ogrinfo(source, encoding)
        layers = meta.get("layers") or []
        if not layers:
            continue
        names = "".join(f.get("name", "") for f in layers[0].get("fields", []))
        if not _MOJIBAKE.search(names):
            return encoding
    return "CP932"


def to_geojsonl(source: Path, dest: Path, source_srs: str = "") -> bool:
    """EPSG:4326 の GeoJSONSeq に変換する。

    平面直角座標系で配布されるので必ず再投影する。系番号は .prj から
    GDAL が読むため個別指定は要らない（.prj が無いときだけ source_srs を使う）。
    DBF は CP932 なので SHAPE_ENCODING で明示する。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ogr2ogr", "-f", "GeoJSONSeq", str(dest), str(source),
           "-t_srs", "EPSG:4326", "-skipfailures", "-makevalid"]
    if source_srs:
        cmd += ["-s_srs", source_srs]
    # 属性名からの推定だけでは決めきれない。DBF のフィールド名は 10 バイト上限で
    # 多バイト文字の途中で切れることがあり（'調査区' が 10 バイト目で分断される）、
    # どちらの符号化でも壊れて見える。CP932 を指定すると GDAL が変換するので
    # 出力は必ず妥当な UTF-8 になるが、UTF-8 を指定すると生バイトが素通りして
    # 不正な UTF-8 が出る。そこで出力の妥当性で最終判断する。
    for encoding in (detect_encoding(source), "CP932"):
        # 残っていると ogr2ogr が「already exists」で落ちる。作り直しでも同じ
        dest.unlink(missing_ok=True)
        try:
            subprocess.run(cmd, capture_output=True, check=True, timeout=1800,
                           env={**_env(), "SHAPE_ENCODING": encoding})
        except subprocess.CalledProcessError as exc:
            log.warning("ogr2ogr 失敗 %s: %s", source.name,
                        exc.stderr.decode("utf-8", "replace")[:300])
            return False
        except subprocess.TimeoutExpired:
            log.warning("ogr2ogr タイムアウト: %s", source.name)
            return False
        if _is_valid_utf8(dest):
            return True
        log.info("%s: %s では不正な UTF-8 になったので CP932 で作り直します",
                 source.name, encoding)
    return True


def _srs_of_prj(prj: Path) -> str:
    """.prj を EPSG コードに解決する。できなければ空。

    同じ座標系でも WKT の書き方が 2 通り出てくる（ESRI 版 JGD_2000_Japan_Zone_7 と
    EPSG 版 JGD2000 / Japan Plane Rectangular CS VII）。文字列のままでは
    別物に見えるので、コードに揃えてから比べる。
    """
    try:
        out = subprocess.run(["gdalsrsinfo", "-o", "epsg", str(prj)],
                             capture_output=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = out.decode("utf-8", "replace").strip()
    return text if text.startswith("EPSG:") else ""


_dataset_srs_cache: dict[Path, str] = {}


def dataset_srs(source: Path, work: Path) -> str:
    """.prj が無いレイヤに、同じデータセット内の .prj から座標系を借りる。

    埼玉県の分割ファイル（さいたま市①〜⑥など）は .shp/.dbf/.shx だけで .prj が無い。
    その場合 ogr2ogr は「source layer has no coordinate system」と言いながら
    **終了コード 0 で 0 件のファイルを書く**ため、失敗として検出できない。
    同じデータセットの他レイヤは 56 件すべて JGD2000 Zone 9 で揃っているので、
    1 つに定まるときだけ借りる。割れているときは推測しない。
    """
    try:
        rel = source.relative_to(work)
    except ValueError:
        return ""
    if len(rel.parts) < 2:
        return ""
    root = work / rel.parts[0] / rel.parts[1]
    if root in _dataset_srs_cache:
        return _dataset_srs_cache[root]
    codes = {code for prj in root.rglob("*.prj") if (code := _srs_of_prj(prj))}
    if len(codes) == 1:
        srs = codes.pop()
        log.info("%s: .prj が無いので同じデータセットの %s を使います", rel, srs)
    else:
        srs = ""
        if codes:
            log.warning("%s: .prj が無く、データセット内の座標系が %d 種類に割れています",
                        rel, len(codes))
    _dataset_srs_cache[root] = srs
    return srs


def is_empty_output(dest: Path) -> bool:
    """変換結果が実質空なら True。

    GeoJSONSeq は 1 行 1 フィーチャなので、中身があれば必ず数十バイト以上になる。
    0〜1 バイトで残っているのは変換の失敗か事故の跡で、そのまま「変換済み」と
    見なすと欠測に気づけない（実際に土地利用 26 レイヤ・448,986 フィーチャが
    空のまま「変換済み」として扱われ、公開データから抜け落ちていた）。
    """
    try:
        return dest.stat().st_size <= 1
    except OSError:
        return True


def _is_valid_utf8(path: Path, sample: int = 1 << 20) -> bool:
    """出力の先頭を読んで UTF-8 として妥当か確かめる。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(sample)
    except OSError:
        return False
    # 末尾で多バイト文字が切れている可能性があるので数バイト削りながら試す
    for cut in range(4):
        try:
            (head if cut == 0 else head[:-cut]).decode("utf-8")
            return True
        except UnicodeDecodeError:
            continue
    return False


def _env() -> dict:
    import os
    return dict(os.environ)


# タイルに入れない属性。GIS ソフトが自動で持つ行番号と、面積・周長の再計算値。
# 実測で土地利用の属性ペイロードの 51% がこれらだった（SHAPE_Area 12.2% /
# SHAPE_Leng 12.1% / PRIMETER 8.8% / OBJECTID 4.7% / ID 2.3% …）。
#
# 面積・周長を落とすのは容量のためだけではない。再投影と簡略化を通った時点で
# タイル上の形と値が合わなくなるので、載せると嘘になる。元データ自身の実測値
# （AREA・面積・lui_area など）は内容なので残す。
DROP_FIELDS = (
    "OBJECTID", "OBJECTID_", "ID", "ID_", "UserID",   # ArcGIS の行番号・編集者記録
    "__ID", "__QID", "__TEXT",                        # ArcInfo カバレッジ由来の内部ID
    "SHAPE_Leng", "SHAPE_Area", "Shape_Leng", "Shape_Area",
    "PRIMETER", "PRIMETER_",                          # 原データの綴りのまま（perimeter）
)


def build_pmtiles(inputs: list[Path], dest: Path, layer_name: str,
                  min_zoom: int = 4, max_zoom: int = 14) -> bool:
    """tippecanoe → pmtiles。

    tippecanoe 2.80 は拡張子が .pmtiles でも中身は mbtiles(SQLite) を書くため、
    pmtiles convert を必ず通す。
    """
    if not inputs:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    mbtiles = dest.with_suffix(".mbtiles")
    # 埼玉県の DBF はフィールド名に \r が入っている（'PRIMETER\r'）。名前は
    # 一致比較なので、素の名前だけ指定しても落ちない。変種も並べて渡す
    exclude = [arg for name in DROP_FIELDS
               for variant in (name, name + "\r")
               for arg in ("-x", variant)]
    cmd = ["tippecanoe", "-o", str(mbtiles), "--force",
           "-l", layer_name, "-Z", str(min_zoom), "-z", str(max_zoom),
           "--drop-densest-as-needed", "--extend-zooms-if-still-dropping",
           "--no-tile-size-limit"] + exclude + [str(p) for p in inputs]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=7200)
    except subprocess.CalledProcessError as exc:
        log.warning("tippecanoe 失敗: %s", exc.stderr.decode("utf-8", "replace")[:300])
        return False
    except subprocess.TimeoutExpired:
        log.warning("tippecanoe タイムアウト")
        return False

    try:
        subprocess.run(["pmtiles", "convert", str(mbtiles), str(dest)],
                       capture_output=True, check=True, timeout=3600)
    except subprocess.CalledProcessError as exc:
        log.warning("pmtiles convert 失敗: %s", exc.stderr.decode("utf-8", "replace")[:300])
        return False
    finally:
        mbtiles.unlink(missing_ok=True)
    return True
