"""CKAN API クライアント。

対象カタログは CKAN 2.x 系だが、Solr の日本語トークナイズ設定がインスタンスごとに
違う。同じ `q="都市計画基礎調査"` でも G空間情報センターは 23 件、フレーズ無しなら
55 件、東京都はフレーズ無しで 2832/9648 件（ほぼ全件＝ノイズ）を返す。
したがって「検索は広めに投げ、絞り込みはクライアント側で行う」方針をとる。
"""

from __future__ import annotations

import logging
import ssl
import time
from dataclasses import dataclass
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

# HTTP ヘッダは latin-1 しか通らないので日本語は入れない。
# 一部カタログ（G空間情報センター）の WAF は見慣れない UA を 403 で弾くため、
# カタログ側で user_agent を上書きできるようにしてある（catalogs.yaml 参照）。
USER_AGENT = "Mozilla/5.0 (compatible; bskp-harvester/0.1)"

# CKAN の package_search が 1 回で返せる上限。これを超えると 500 を返す実装がある
PAGE_SIZE = 100


class CkanError(RuntimeError):
    pass


class LegacyTLSAdapter(requests.adapters.HTTPAdapter):
    """OpenSSL 3 の既定より緩い暗号方式を許可するアダプタ。

    山口県オープンデータカタログは TLSV1_ALERT_INSUFFICIENT_SECURITY を返す
    （鍵長が OpenSSL 3 の既定 SECLEVEL=2 に届かない）。curl は通るので
    見落としやすい。読み取り専用・認証情報なしの公開カタログに限って使う想定で、
    catalogs.yaml の legacy_tls: true を書いたカタログにだけ適用する。
    """

    def init_poolmanager(self, *args, **kwargs):  # type: ignore[override]
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


@dataclass
class Catalog:
    id: str
    name: str
    api: str
    site: str = ""
    enabled: bool = True
    note: str = ""
    user_agent: str = ""
    legacy_tls: bool = False

    @property
    def action_base(self) -> str:
        return self.api.rstrip("/") + "/api/3/action"

    def dataset_url(self, name: str) -> str:
        """データセットの人間向け URL。CKAN の慣例に従い /dataset/<name>。"""
        return f"{self.api.rstrip('/')}/dataset/{name}"


class CkanClient:
    def __init__(self, catalog: Catalog, timeout: int = 60, retries: int = 3,
                 pause: float = 0.5):
        self.catalog = catalog
        self.timeout = timeout
        self.retries = retries
        self.pause = pause
        self.session = requests.Session()
        self.session.headers["User-Agent"] = catalog.user_agent or USER_AGENT
        if catalog.legacy_tls:
            self.session.mount("https://", LegacyTLSAdapter())

    def _call(self, action: str, **params: Any) -> Any:
        url = f"{self.catalog.action_base}/{action}"
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                r.raise_for_status()
                body = r.json()
                if not body.get("success"):
                    raise CkanError(f"{action} returned success=false: {body.get('error')}")
                return body["result"]
            except Exception as exc:  # noqa: BLE001 - リトライしてから諦める
                last = exc
                if attempt < self.retries:
                    wait = self.pause * 2 ** (attempt - 1)
                    log.debug("%s: %s (retry %d/%d in %.1fs)",
                              self.catalog.id, exc, attempt, self.retries, wait)
                    time.sleep(wait)
        raise CkanError(f"{self.catalog.id}: {action} failed: {last}") from last

    def ping(self) -> int:
        """疎通確認。カタログの総データセット数を返す。"""
        return self._call("package_search", q="", rows=0)["count"]

    def search(self, query: str, limit: int | None = None) -> Iterator[dict]:
        """package_search をページングしながら全件流す。

        limit は「取得を打ち切る件数」。ヒット数が数千件になるカタログがあるため、
        呼び出し側でフィルタする前提の粗い検索では上限を付ける。
        打ち切りが起きたら WARNING を出す（黙って取りこぼさないため）。
        """
        start = 0
        total: int | None = None
        while True:
            rows = PAGE_SIZE
            if limit is not None:
                rows = min(rows, limit - start)
                if rows <= 0:
                    log.warning("%s: q=%r truncated at %d of %d hits "
                                "(--limit 0 で全件走査できます)",
                                self.catalog.id, query, limit, total)
                    return
            result = self._call("package_search", q=query, rows=rows, start=start)
            if total is None:
                total = result["count"]
                log.info("%s: q=%r -> %d hits", self.catalog.id, query, total)
            batch = result.get("results", [])
            if not batch:
                return
            yield from batch
            start += len(batch)
            if start >= total:
                return
            time.sleep(self.pause)
