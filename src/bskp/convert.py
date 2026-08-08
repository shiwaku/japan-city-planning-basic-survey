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


def _ogrinfo(path: Path) -> dict:
    """ogrinfo -so -al -json でレイヤの素性を読む。

    SHAPE_ENCODING を渡しても、DBF 由来のフィールド名がそのまま CP932 バイトで
    出てくることがあり、json.loads がそこで落ちる。素性（SRS・件数・型）が
    取れれば十分なので、デコードできないバイトは置換して読み進める。
    """
    try:
        out = subprocess.run(
            ["ogrinfo", "-so", "-al", "-json", str(path)],
            capture_output=True, timeout=180, check=True,
            env={**_env(), "SHAPE_ENCODING": "CP932"},
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
    env_note = {"SHAPE_ENCODING": "CP932"}
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=1800,
                       env={**_env(), **env_note})
        return True
    except subprocess.CalledProcessError as exc:
        log.warning("ogr2ogr 失敗 %s: %s", source.name,
                    exc.stderr.decode("utf-8", "replace")[:300])
    except subprocess.TimeoutExpired:
        log.warning("ogr2ogr タイムアウト: %s", source.name)
    return False


def _env() -> dict:
    import os
    return dict(os.environ)


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
    cmd = ["tippecanoe", "-o", str(mbtiles), "--force",
           "-l", layer_name, "-Z", str(min_zoom), "-z", str(max_zoom),
           "--drop-densest-as-needed", "--extend-zooms-if-still-dropping",
           "--no-tile-size-limit"] + [str(p) for p in inputs]
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
