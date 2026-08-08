import { createReadStream, statSync } from 'node:fs'
import { extname, join, normalize, resolve } from 'node:path'
import type { Connect, Plugin } from 'vite'
import { defineConfig } from 'vite'

// PMTiles はリポジトリ直下の data/tiles にある（viewer の外）。
// Vite の root 外なので静的配信されない。開発時だけ /tiles/* を中継する。
//
// PMTiles は Range リクエストで必要なタイルだけ読む形式なので、Range 対応が必須。
// 対応しないと 1 テーマ開くたびにファイル全体（自然環境は 34 MiB）が飛ぶ。
const TILES_DIR = resolve(__dirname, '..', 'data', 'tiles')

const MIME: Record<string, string> = {
  '.pmtiles': 'application/octet-stream',
  '.json': 'application/json',
}

function serveTiles(): Plugin {
  const middleware: Connect.NextHandleFunction = (req, res, next) => {
    const url = req.url ?? ''
    if (!url.startsWith('/tiles/')) return next()

    // ディレクトリ外への脱出を防ぐ
    const rel = normalize(decodeURIComponent(url.slice('/tiles/'.length))).replace(/^(\.\.[/\\])+/, '')
    const file = join(TILES_DIR, rel)
    if (!file.startsWith(TILES_DIR)) {
      res.statusCode = 403
      return res.end('forbidden')
    }

    let size: number
    try {
      size = statSync(file).size
    } catch {
      res.statusCode = 404
      return res.end('not found')
    }

    res.setHeader('Content-Type', MIME[extname(file)] ?? 'application/octet-stream')
    res.setHeader('Accept-Ranges', 'bytes')

    const range = /^bytes=(\d*)-(\d*)$/.exec((req.headers.range ?? '').trim())
    if (!range) {
      res.setHeader('Content-Length', String(size))
      return createReadStream(file).pipe(res)
    }

    const [, first, last] = range
    const start = first ? Number(first) : Math.max(0, size - Number(last || 0))
    const end = Math.min(first ? (last ? Number(last) : size - 1) : size - 1, size - 1)
    if (start > end || start >= size) {
      res.statusCode = 416
      res.setHeader('Content-Range', `bytes */${size}`)
      return res.end()
    }

    res.statusCode = 206
    res.setHeader('Content-Range', `bytes ${start}-${end}/${size}`)
    res.setHeader('Content-Length', String(end - start + 1))
    createReadStream(file, { start, end }).pipe(res)
  }

  return {
    name: 'serve-tiles',
    configureServer: (server) => void server.middlewares.use(middleware),
    configurePreviewServer: (server) => void server.middlewares.use(middleware),
  }
}

export default defineConfig({
  base: './',
  plugins: [serveTiles()],
  build: { target: 'es2022' },
})
