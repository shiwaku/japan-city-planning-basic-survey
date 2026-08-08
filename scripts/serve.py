#!/usr/bin/env python3
"""web/index.html を動かすための開発用サーバ。

PMTiles は HTTP Range リクエストで必要なタイルだけ読む形式だが、
Python 標準の SimpleHTTPRequestHandler は Range に対応しておらず、
毎回ファイル全体（自然環境なら 34 MiB）を返してしまう。
そのため Range 対応を足した最小のサーバを用意する。

    python scripts/serve.py          # http://localhost:8000/web/ を開く
"""

from __future__ import annotations

import argparse
import os
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class RangeHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):  # noqa: ANN201 - 親クラスに合わせる
        header = self.headers.get("Range")
        if not header:
            return super().send_head()

        match = RANGE_RE.fullmatch(header.strip())
        if not match:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            fh = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        size = os.fstat(fh.fileno()).st_size
        first, last = match.group(1), match.group(2)
        if first:
            start = int(first)
            end = int(last) if last else size - 1
        else:
            # bytes=-N は末尾 N バイト
            start = max(0, size - int(last or 0))
            end = size - 1
        end = min(end, size - 1)

        if start > end or start >= size:
            fh.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None

        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        fh.seek(start)
        self._remaining = end - start + 1
        return _Limited(fh, self._remaining)


class _Limited:
    """copyfile が読み過ぎないよう、指定バイト数で止めるラッパ。"""

    def __init__(self, fh, remaining: int):
        self.fh = fh
        self.remaining = remaining

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if size is None or size < 0:
            size = self.remaining
        data = self.fh.read(min(size, self.remaining))
        self.remaining -= len(data)
        return data

    def close(self) -> None:
        self.fh.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--root", default=os.path.join(os.path.dirname(__file__), ".."))
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    handler = partial(RangeHandler, directory=root)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"{root} を配信中")
    print(f"  http://localhost:{args.port}/web/  を開いてください（Ctrl+C で停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")


if __name__ == "__main__":
    main()
