import maplibregl from 'maplibre-gl'
import { Protocol } from 'pmtiles'
import 'maplibre-gl/dist/maplibre-gl.css'

import {
  GROUP_UNCLASSIFIED, LANDUSE_GROUPS, MAX_ACTIVE, THEMES, UNCLASSIFIED,
  colorOf, landuseColorExpression, themeOf, type ThemeDef,
} from './layers'
import { applyThemeAttr, initialTheme, type Theme } from './theme'
import './style.css'

interface TileEntry {
  theme: string
  slug: string
  file: string
  layers: number
  bytes: number
}

/** bskp tiles が実データから生成する出典情報。CC-BY / GNU FDL とも表示が必須。 */
interface Attribution {
  organization: string
  license: string
  catalog: string
  datasets: number
  url: string
}

let theme: Theme = initialTheme()
applyThemeAttr(theme)

const protocol = new Protocol()
maplibregl.addProtocol('pmtiles', protocol.tile)

// ---- 背景地図 ----
// 地理院淡色タイル。ダーク時は raster paint で沈める（別スタイルを持たない）。
function basemapStyle(mode: Theme): maplibregl.StyleSpecification {
  return {
    version: 8,
    sources: {
      gsi: {
        type: 'raster',
        tiles: ['https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png'],
        tileSize: 256,
        maxzoom: 18,
        attribution:
          '<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank" rel="noopener">地理院タイル</a>',
      },
    },
    layers: [
      {
        id: 'basemap',
        type: 'raster',
        source: 'gsi',
        paint:
          mode === 'dark'
            ? { 'raster-brightness-max': 0.42, 'raster-saturation': -0.5, 'raster-contrast': 0.1 }
            : {},
      },
    ],
  }
}

const map = new maplibregl.Map({
  container: 'map',
  hash: true,
  center: [137.5, 34.9],
  zoom: 8,
  attributionControl: { compact: true },
  style: basemapStyle(theme),
})
map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), 'top-right')
map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: 'metric' }))
map.addControl(new maplibregl.GeolocateControl({ trackUserLocation: true }), 'top-right')

// ---- 状態 ----
const active = new Set(THEMES.filter((t) => t.on).map((t) => t.key))
const available = new Map<string, TileEntry>()
const opacity = new Map<string, number>(THEMES.map((t) => [t.key, 0.45]))

const srcId = (key: string): string => `src-${key}`
const fillId = (key: string): string => `${key}-fill`
const lineId = (key: string): string => `${key}-line`
const pointId = (key: string): string => `${key}-point`
const allLayerIds = (key: string): string[] =>
  [fillId(key), lineId(key), pointId(key), `${key}-hatch`]

const interactiveIds = (): string[] =>
  [...active].flatMap(allLayerIds).filter((id) => map.getLayer(id))

/** 未分類を塗り分けるための斜線ハッチ。色に頼らない手掛かりとして使う。 */
function ensureHatch(): void {
  const id = 'hatch-unclassified'
  if (map.hasImage(id)) map.removeImage(id)
  const size = 8
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = size
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.strokeStyle = UNCLASSIFIED[theme]
  ctx.lineWidth = 1.5
  ctx.beginPath()
  ctx.moveTo(-size, size); ctx.lineTo(size, -size)
  ctx.moveTo(0, size * 2); ctx.lineTo(size * 2, 0)
  ctx.stroke()
  map.addImage(id, ctx.getImageData(0, 0, size, size), { pixelRatio: 1 })
}

/**
 * データレイヤを追加する。ジオメトリ型が混在した PMTiles なので、
 * 面・線・点の 3 レイヤを geometry-type で振り分けて重ねる。
 */
function addDataLayers(): void {
  for (const def of THEMES) {
    if (!available.has(def.key)) continue
    const color = colorOf(def, theme)
    const visible = active.has(def.key) ? 'visible' : 'none'
    const alpha = opacity.get(def.key) ?? 0.45

    if (!map.getSource(srcId(def.key))) {
      map.addSource(srcId(def.key), {
        type: 'vector',
        url: `pmtiles://${tilesBase}/${def.key}.pmtiles`,
      })
    }

    if (def.key === 'landuse') {
      // 土地利用だけは単色ではなく、正規化した lui_group で 3 系統に塗り分ける
      ensureHatch()
      map.addLayer({
        id: `${def.key}-hatch`, type: 'fill', source: srcId(def.key), 'source-layer': def.key,
        filter: ['all', ['==', ['geometry-type'], 'Polygon'],
                 ['==', ['get', 'lui_group'], GROUP_UNCLASSIFIED]],
        layout: { visibility: visible },
        paint: { 'fill-pattern': 'hatch-unclassified', 'fill-opacity': Math.min(1, alpha + 0.35) },
      })
      map.addLayer({
        id: fillId(def.key), type: 'fill', source: srcId(def.key), 'source-layer': def.key,
        filter: ['all', ['==', ['geometry-type'], 'Polygon'],
                 ['!=', ['get', 'lui_group'], GROUP_UNCLASSIFIED]],
        layout: { visibility: visible },
        paint: {
          'fill-color': landuseColorExpression(theme) as never,
          'fill-opacity': alpha,
        },
      })
    } else {
      map.addLayer({
        id: fillId(def.key), type: 'fill', source: srcId(def.key), 'source-layer': def.key,
        filter: ['==', ['geometry-type'], 'Polygon'],
        layout: { visibility: visible },
        paint: { 'fill-color': color, 'fill-opacity': alpha, 'fill-outline-color': color },
      })
    }
    map.addLayer({
      id: lineId(def.key), type: 'line', source: srcId(def.key), 'source-layer': def.key,
      filter: ['==', ['geometry-type'], 'LineString'],
      layout: { visibility: visible, 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': color, 'line-width': 2, 'line-opacity': Math.min(1, alpha + 0.35) },
    })
    map.addLayer({
      id: pointId(def.key), type: 'circle', source: srcId(def.key), 'source-layer': def.key,
      filter: ['==', ['geometry-type'], 'Point'],
      layout: { visibility: visible },
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 3, 14, 5],
        'circle-color': color,
        'circle-opacity': Math.min(1, alpha + 0.35),
        'circle-stroke-width': 1.5,
        'circle-stroke-color': theme === 'dark' ? '#1b1e24' : '#ffffff',
      },
    })
  }
}

// ---- テーマ切替 ----
const themeBtn = document.getElementById('theme-btn') as HTMLButtonElement
const renderThemeBtn = (): void => {
  themeBtn.textContent = theme === 'dark' ? '☀' : '☾'
  themeBtn.title = theme === 'dark' ? 'ライトに切替' : 'ダークに切替'
}
themeBtn.addEventListener('click', () => {
  theme = theme === 'dark' ? 'light' : 'dark'
  applyThemeAttr(theme)
  renderThemeBtn()
  // 背景もデータ色も差し替わるのでスタイルごと作り直す
  map.setStyle(basemapStyle(theme))
  map.once('styledata', () => {
    addDataLayers()
    buildToggles()
  })
})

// ---- パネル開閉 ----
const panel = document.getElementById('panel') as HTMLElement
const collapseBtn = document.getElementById('collapse-btn') as HTMLButtonElement
collapseBtn.addEventListener('click', () => {
  const collapsed = panel.classList.toggle('collapsed')
  collapseBtn.textContent = collapsed ? '▸' : '▾'
  collapseBtn.setAttribute('aria-expanded', String(!collapsed))
})

// ---- レイヤートグル ----
const layersDiv = document.getElementById('layers') as HTMLElement
const noticeEl = document.getElementById('notice') as HTMLElement

function canEnable(): boolean {
  return active.size < MAX_ACTIVE
}

function showNotice(msg: string): void {
  noticeEl.textContent = msg
  noticeEl.classList.add('show')
  window.setTimeout(() => noticeEl.classList.remove('show'), 3200)
}

function buildToggles(): void {
  layersDiv.textContent = ''
  for (const def of THEMES) {
    const entry = available.get(def.key)
    const row = document.createElement('div')
    row.className = 'layer' + (entry ? '' : ' missing')

    const on = active.has(def.key)
    row.innerHTML = `
      <label class="layer-head">
        <input type="checkbox" ${on ? 'checked' : ''} ${entry ? '' : 'disabled'}>
        <span class="swatch" style="background:${colorOf(def, theme)}"></span>
        <span class="layer-name">${def.name}</span>
        <span class="layer-count">${entry ? `${entry.layers}層` : '—'}</span>
      </label>
      <div class="layer-body">
        <p class="layer-desc">${def.desc}</p>
        ${def.key === 'landuse' ? landuseLegend() : ''}
        <label class="opacity">
          <span>不透明度</span>
          <input type="range" min="0.05" max="1" step="0.05"
                 value="${opacity.get(def.key)}" ${entry ? '' : 'disabled'}>
        </label>
      </div>`

    const checkbox = row.querySelector('input[type=checkbox]') as HTMLInputElement
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) {
        if (!canEnable()) {
          checkbox.checked = false
          showNotice(
            `同時表示は${MAX_ACTIVE}項目までです。配色が識別可能なのが${MAX_ACTIVE}色までのため、` +
            'どれかを外してから選んでください。',
          )
          return
        }
        active.add(def.key)
      } else {
        active.delete(def.key)
      }
      setVisible(def, checkbox.checked)
      row.classList.toggle('active', checkbox.checked)
    })

    const slider = row.querySelector('input[type=range]') as HTMLInputElement
    slider.addEventListener('input', () => {
      const v = Number(slider.value)
      opacity.set(def.key, v)
      setOpacity(def, v)
    })

    row.classList.toggle('active', on)
    layersDiv.appendChild(row)
  }
}

/**
 * 土地利用の凡例。3 系統の色と、未分類のハッチを並べる。
 * 識別を色だけに依存させないため、必ず名前を添える。
 */
function landuseLegend(): string {
  const items = LANDUSE_GROUPS.map(
    (g) => `<li><span class="key" style="background:${g[theme]}"></span>${g.value}</li>`,
  ).join('')
  return `<ul class="legend">${items}
    <li><span class="key hatch" style="--hatch:${UNCLASSIFIED[theme]}"></span>
        ${GROUP_UNCLASSIFIED}<span class="legend-note">対照表に記載なし</span></li>
  </ul>
  <p class="legend-note">国土交通省の対照表で全国共通コードに正規化し、
     実施要領の大分類（自然的／都市的／低未利用）に集約した区分。</p>`
}


function setVisible(def: ThemeDef, on: boolean): void {
  for (const id of allLayerIds(def.key)) {
    if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none')
  }
}

function setOpacity(def: ThemeDef, v: number): void {
  if (map.getLayer(fillId(def.key))) map.setPaintProperty(fillId(def.key), 'fill-opacity', v)
  const strong = Math.min(1, v + 0.35)
  if (map.getLayer(lineId(def.key))) map.setPaintProperty(lineId(def.key), 'line-opacity', strong)
  if (map.getLayer(pointId(def.key))) map.setPaintProperty(pointId(def.key), 'circle-opacity', strong)
}

// ---- クリックで属性表示 ----
map.on('click', (ev) => {
  const ids = interactiveIds()
  if (!ids.length) return
  const hits = map.queryRenderedFeatures(ev.point, { layers: ids })
  if (!hits.length) return

  const f = hits[0]
  const def = themeOf(f.layer.id.replace(/-(fill|line|point)$/, ''))
  const rows = Object.entries(f.properties ?? {})
    .filter(([, v]) => v !== null && v !== '')
    .map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(String(v))}</td></tr>`)
    .join('')

  new maplibregl.Popup({ maxWidth: '320px', closeButton: true })
    .setLngLat(ev.lngLat)
    .setHTML(
      `<div class="popup"><h2>${esc(def?.name ?? '')}</h2>` +
      `<table>${rows || '<tr><td>属性なし</td></tr>'}</table></div>`,
    )
    .addTo(map)
})

map.on('mousemove', (ev) => {
  const ids = interactiveIds()
  const hit = ids.length && map.queryRenderedFeatures(ev.point, { layers: ids }).length
  map.getCanvas().style.cursor = hit ? 'pointer' : ''
})

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string)
}

// ---- 起動 ----
// dev  : vite.config.ts の middleware が ../data/tiles を /tiles に中継する
// build: 既定は dist と同階層の tiles/。data/tiles をそこへコピーするか
//        シンボリックリンクを張る（Makefile の `make viewer` がやる）。
//        別ホストに置くなら VITE_TILES_BASE で URL を渡す。
const tilesBase = import.meta.env.DEV
  ? '/tiles'
  : (import.meta.env.VITE_TILES_BASE ?? './tiles')
const statusEl = document.getElementById('status') as HTMLElement

async function boot(): Promise<void> {
  let index: TileEntry[] = []
  try {
    const res = await fetch(`${tilesBase}/index.json`)
    if (!res.ok) throw new Error(String(res.status))
    index = await res.json()
  } catch {
    statusEl.textContent =
      'タイルを読み込めません。bskp tiles を実行して data/tiles を作ってください。'
    buildToggles()
    return
  }

  for (const entry of index) available.set(entry.slug, entry)
  // データが無いテーマは ON のままにしない
  for (const key of [...active]) if (!available.has(key)) active.delete(key)

  await new Promise<void>((r) => (map.loaded() ? r() : map.once('load', () => r())))
  addDataLayers()
  buildToggles()

  const layers = index.reduce((a, e) => a + e.layers, 0)
  const mib = index.reduce((a, e) => a + e.bytes, 0) / 1048576
  statusEl.textContent = `${index.length}項目 / ${layers}レイヤ / ${mib.toFixed(0)} MiB`

  await renderAttribution()
}

/**
 * 出典表示。CC-BY も GNU FDL も再配布には表示が必要なので、
 * 実データから生成した attribution.json をそのまま出す（手書きにしない）。
 */
async function renderAttribution(): Promise<void> {
  const el = document.getElementById('attribution') as HTMLElement
  try {
    const res = await fetch(`${tilesBase}/attribution.json`)
    if (!res.ok) throw new Error(String(res.status))
    const list: Attribution[] = await res.json()
    // 提供元は50件を超えるのでライセンス単位にまとめる。
    // 全件は details に畳んでおく（CC-BY も GFDL も表示義務があるため省略はしない）
    const byLicense = new Map<string, string[]>()
    for (const a of list) {
      const key = a.license || 'ライセンス表記なし'
      const orgs = byLicense.get(key) ?? []
      orgs.push(a.organization)
      byLicense.set(key, orgs)
    }
    const summary = [...byLicense]
      .sort((a, b) => b[1].length - a[1].length)
      .map(([lic, orgs]) => `${esc(lic)}（${orgs.length}団体）`)
      .join(' / ')
    const detail = [...byLicense]
      .map(([lic, orgs]) =>
        `<dt>${esc(lic)}</dt><dd>${orgs.map(esc).join('、')}</dd>`)
      .join('')
    el.innerHTML =
      `出典: ${summary}` +
      `<details class="sources"><summary>提供元の一覧（${list.length}件）</summary>` +
      `<dl>${detail}</dl></details>`
  } catch {
    el.textContent = '出典: 各自治体が公開する都市計画基礎調査オープンデータ'
  }
}

renderThemeBtn()
void boot()
