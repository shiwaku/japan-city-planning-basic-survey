"""CKAN API を持たない配布サイトから、データファイルへのリンクを拾う。

CKAN 横断で届かない県が実在する（広島県は DoboX という独自ポータルで配布しており、
CKAN API は無く `/api/datasets` は 401 を返す）。そういう相手はページを読んで
リンクを拾うしかない。

出力は harvest と同じ ResourceRow スキーマ（data/inventory/scraped.csv）なので、
`bskp fetch` はカタログ由来と区別なく扱える。

方針:
  - robots.txt を必ず確認し、禁止されていれば取得しない
  - 1 ドメインあたり直列・ウェイト付き。並列で叩かない
  - 深さは sites.yaml で明示した範囲のみ。無制限クロールはしない
"""

from __future__ import annotations

import logging
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from .ckan import USER_AGENT
from .harvest import ResourceRow, classify_format, themes_for

log = logging.getLogger(__name__)

# 拾う対象の拡張子。これ以外はデータファイルとみなさない
DATA_EXT = re.compile(
    r"\.(zip|7z|lzh|shp|geojson|json|kml|kmz|gml|gpkg|csv|tsv|xlsx?|pdf)(\?|$)", re.I)


@dataclass
class Site:
    id: str
    name: str
    start_urls: list[str]
    prefecture: str = ""
    enabled: bool = True
    note: str = ""
    license: str = ""
    # このパターンに一致するリンクは「次に読むページ」として辿る（1 段だけ）
    follow: str = ""
    # リンクテキストか URL がこれに一致するものだけ採用（空なら拡張子判定のみ）
    match: str = ""
    pause: float = 1.0
    headers: dict = field(default_factory=dict)


class LinkParser(HTMLParser):
    """<a href> を (href, リンクテキスト) の組で集める最小限のパーサ。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._href = href
                self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text).strip()))
            self._href = None


_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset=["']?\s*([\w\-]+)""", re.I)


def _decode(response: requests.Response) -> str:
    """自治体サイトは Shift_JIS も UTF-8 も混在し、Content-Type が嘘のこともある。

    requests は charset 未指定の text/html を ISO-8859-1 と決め打ちするので、
    そのまま .text を読むとリンク文字列が化ける（津島市で実際に化けた）。
    HTML の meta charset を最優先し、次に実バイト列からの推定、最後に UTF-8。
    """
    raw = response.content
    candidates: list[str] = []

    m = _META_CHARSET.search(raw[:4096])
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))

    declared = response.encoding
    if declared and declared.lower() not in {"iso-8859-1", "ascii"}:
        candidates.append(declared)

    candidates += [response.apparent_encoding or "", "utf-8", "cp932"]

    for enc in candidates:
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


class RobotsGate:
    """ドメインごとに robots.txt を 1 回だけ読んで判定をキャッシュする。"""

    def __init__(self, session: requests.Session):
        self.session = session
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str, user_agent: str) -> bool:
        root = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if root not in self._cache:
            parser = urllib.robotparser.RobotFileParser()
            try:
                r = self.session.get(root + "/robots.txt", timeout=20)
                if r.status_code == 200:
                    parser.parse(r.text.splitlines())
                else:
                    parser = None  # robots.txt が無い＝制限なしとみなす
            except Exception as exc:  # noqa: BLE001
                log.debug("robots.txt unreadable for %s: %s", root, exc)
                parser = None
            self._cache[root] = parser
            log.info("robots.txt: %s -> %s", root,
                     "loaded" if parser else "none (allow all)")
        parser = self._cache[root]
        return True if parser is None else parser.can_fetch(user_agent, url)


def scrape_site(site: Site) -> list[ResourceRow]:
    session = requests.Session()
    ua = site.headers.get("User-Agent", USER_AGENT)
    session.headers["User-Agent"] = ua
    session.headers.update(site.headers)
    gate = RobotsGate(session)

    match_re = re.compile(site.match) if site.match else None
    follow_re = re.compile(site.follow) if site.follow else None

    pages = list(site.start_urls)
    visited: set[str] = set()
    found: dict[str, tuple[str, str]] = {}  # url -> (リンクテキスト, 出典ページ)
    queued_follow: list[str] = []

    def read(url: str) -> list[tuple[str, str]]:
        if url in visited:
            return []
        visited.add(url)
        if not gate.allowed(url, ua):
            log.warning("%s: robots.txt により取得しません: %s", site.id, url)
            return []
        try:
            r = session.get(url, timeout=45)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: %s の取得に失敗: %s", site.id, url, exc)
            return []
        parser = LinkParser()
        parser.feed(_decode(r))
        time.sleep(site.pause)
        return [(urljoin(url, h), t) for h, t in parser.links]

    for page in pages:
        for href, text in read(page):
            if DATA_EXT.search(href):
                if match_re and not (match_re.search(href) or match_re.search(text)):
                    continue
                found.setdefault(href, (text, page))
            elif follow_re and follow_re.search(href):
                queued_follow.append(href)

    for page in queued_follow:
        for href, text in read(page):
            if DATA_EXT.search(href):
                if match_re and not (match_re.search(href) or match_re.search(text)):
                    continue
                found.setdefault(href, (text, page))

    rows = []
    for url, (text, src) in found.items():
        ext = DATA_EXT.search(url)
        fmt = (ext.group(1).upper() if ext else "")
        name = text or url.rsplit("/", 1)[-1]
        rows.append(ResourceRow(
            catalog_id=site.id,
            catalog_name=site.name,
            organization=site.prefecture or site.name,
            dataset_name=site.id,
            dataset_title=site.name,
            dataset_url=src,
            license=site.license,
            resource_id="",
            resource_name=name,
            format=fmt,
            size="",
            themes="|".join(themes_for(name, site.name)),
            kind=classify_format(fmt),
            match="scraped",
            url=url,
        ))
    log.info("%s: %d ページ読んで %d ファイルリンクを取得", site.id, len(visited), len(rows))
    return rows
