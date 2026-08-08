"""インベントリの resources.csv を入力に、実ファイルを取得する。

保存先は data/raw/<catalog_id>/<dataset_name>/<連番>_<ファイル名>。
同名リソースが同じデータセットに複数あるカタログ（静岡県の 03_*.zip 群など）が
あるため、CSV の行番号を連番に使って衝突を避ける。

取得済みかどうかは manifest.jsonl の sha256 で判定するので、再実行は差分だけになる。
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import socket
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from .ckan import USER_AGENT

log = logging.getLogger(__name__)

CHUNK = 1 << 20  # 1 MiB
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def prefer_ipv4() -> None:
    """名前解決の結果を IPv4 に絞る。

    東京都の配信ホスト（data.storage.data.metro.tokyo.lg.jp）は AAAA を持つが
    IPv6 では接続が確立せず、そのまま無応答になる（curl -4 なら 206 が返る）。
    名前解決は成功するので「取得中のまま1バイトも進まない」形で現れ、原因が分かりにくい。
    取得先は公開データの配信サーバに限られるので、IPv4 に固定して回避する。
    """
    if getattr(socket, "_bskp_ipv4_only", False):
        return
    original = socket.getaddrinfo

    def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        return original(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = ipv4_only
    socket._bskp_ipv4_only = True  # type: ignore[attr-defined]
    log.debug("名前解決を IPv4 に固定しました")


def safe_name(name: str, fallback: str = "file") -> str:
    """Windows でも作れるファイル/ディレクトリ名にする（WSL 経由で NTFS に置くため）。"""
    cleaned = _UNSAFE.sub("_", unquote(name)).strip(" .")
    return cleaned[:120] or fallback


def filename_from_url(url: str, fallback: str) -> str:
    tail = Path(urlparse(url).path).name
    return safe_name(tail or fallback, fallback)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest(path: Path) -> dict[str, dict]:
    """取得済みリソースを path キーで返す。失敗記録は除くので次回は再試行される。"""
    if not path.exists():
        return {}
    entries: dict[str, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("error"):
                entries.pop(rec["path"], None)
            else:
                entries[rec["path"]] = rec
    return entries


def append_manifest(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def download(url: str, dest: Path, session: requests.Session, timeout: int = 120) -> int:
    """一時ファイルに落としてから rename する。中断しても壊れたファイルが残らない。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with session.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        total = 0
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(CHUNK):
                if chunk:
                    fh.write(chunk)
                    total += len(chunk)
    tmp.replace(dest)
    return total


def fetch_all(rows: list[dict], raw_dir: Path, *, kinds: set[str] | None = None,
              max_bytes: int | None = None, pause: float = 0.5,
              dry_run: bool = False, limit: int | None = None,
              user_agents: dict[str, str] | None = None) -> None:
    """user_agents は catalog_id -> UA。配信側の WAF が UA を見るカタログがある
    （G空間情報センターは既定 UA を 403 で弾く）ので、検索時と同じ UA を使う。"""
    prefer_ipv4()
    manifest_path = raw_dir / "manifest.jsonl"
    done = load_manifest(manifest_path)
    user_agents = user_agents or {}

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    selected = []
    for idx, row in enumerate(rows):
        if kinds and row["kind"] not in kinds:
            continue
        if not row["url"]:
            continue
        if max_bytes and row["size"].isdigit() and int(row["size"]) > max_bytes:
            log.info("skip (too large: %s bytes): %s", row["size"], row["resource_name"])
            continue
        selected.append((idx, row))
        if limit and len(selected) >= limit:
            break

    log.info("%d/%d resources selected for download", len(selected), len(rows))
    total_bytes = 0
    for n, (idx, row) in enumerate(selected, 1):
        rel = Path(safe_name(row["catalog_id"])) / safe_name(row["dataset_name"]) / (
            f"{idx:04d}_{filename_from_url(row['url'], row['resource_id'] or 'file')}"
        )
        dest = raw_dir / rel
        key = rel.as_posix()

        if key in done and dest.exists():
            log.debug("[%d/%d] cached: %s", n, len(selected), key)
            continue
        if dry_run:
            print(f"{row['size'] or '?':>10}  {key}  <- {row['url']}")
            continue

        session.headers["User-Agent"] = user_agents.get(row["catalog_id"], USER_AGENT)
        try:
            size = download(row["url"], dest, session)
        except Exception as exc:  # noqa: BLE001 - 1 件の失敗で全体を止めない
            log.warning("[%d/%d] FAILED %s: %s", n, len(selected), row["url"], exc)
            append_manifest(manifest_path, {
                "path": key, "url": row["url"], "error": str(exc),
            })
            continue

        total_bytes += size
        append_manifest(manifest_path, {
            "path": key,
            "url": row["url"],
            "bytes": size,
            "sha256": sha256_of(dest),
            "catalog_id": row["catalog_id"],
            "dataset_name": row["dataset_name"],
            "dataset_title": row["dataset_title"],
            "resource_name": row["resource_name"],
            "format": row["format"],
            "license": row["license"],
        })
        log.info("[%d/%d] %8.1f MiB  %s", n, len(selected), size / 1048576, key)
        time.sleep(pause)

    log.info("downloaded %.1f MiB in this run", total_bytes / 1048576)
