import maplibregl from 'maplibre-gl'
import { Protocol } from 'pmtiles'
import 'maplibre-gl/dist/maplibre-gl.css'

import { MAX_ACTIVE, THEMES, colorOf, themeOf, type ThemeDef } from './layers'
import {
  BUILDING, BUILDING_ORDER, CONVENTIONAL, CONVENTIONAL_ORDER, UNCLASSIFIED,
  buildingColor, fillColor,
} from './palettes'
import { labelOf } from './field-names'
import { getBasemapStyle, type Basemap } from './basemap'
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

// 建物の LOD1 表示。傾けていないと立体が見えないので、地図の pitch と連動させる
let pitched = localStorage.getItem('bskp-pitch') === 'on'

const protocol = new Protocol()
maplibregl.addProtocol('pmtiles', protocol.tile)

// ---- 背景地図 ----
// 参考実装（mlit-urban-planning-converter/viewer）と同じ地理院最適化ベクトルタイル。
// pale-style.json の source は pmtiles:// なので、上で登録したプロトコルが必要。
// ダークは色を反転して生成する（basemap.ts）。写真は右下のボタンで切り替える。
let basemap: Basemap = (localStorage.getItem('bskp-basemap') as Basemap) || 'pale'

const map = new maplibregl.Map({
  container: 'map',
  hash: true,
  center: [137.5, 34.9],
  zoom: 8,
  attributionControl: { compact: true },
  style: getBasemapStyle(basemap, theme),
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
const extrudeId = (key: string): string => `${key}-3d`
const allLayerIds = (key: string): string[] =>
  [fillId(key), lineId(key), pointId(key), `${key}-hatch`, extrudeId(key)]

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
      // 土地利用だけは単色ではなく、正規化した lui_code で用途別に塗り分ける。
      // 写せなかったものは色を持たせずハッチで描く（lui_code が空）
      ensureHatch()
      map.addLayer({
        id: `${def.key}-hatch`, type: 'fill', source: srcId(def.key), 'source-layer': def.key,
        filter: ['all', ['==', ['geometry-type'], 'Polygon'],
                 ['==', ['get', 'lui_code'], '']],
        layout: { visibility: visible },
        paint: { 'fill-pattern': 'hatch-unclassified', 'fill-opacity': Math.min(1, alpha + 0.35) },
      })
      map.addLayer({
        id: fillId(def.key), type: 'fill', source: srcId(def.key), 'source-layer': def.key,
        filter: ['all', ['==', ['geometry-type'], 'Polygon'],
                 ['!=', ['get', 'lui_code'], '']],
        layout: { visibility: visible },
        paint: {
          'fill-color': fillColor(theme) as never,
          'fill-opacity': alpha,
        },
      })
    } else if (def.key === 'building') {
      // 建物も単色ではなく、正規化した bui_code で用途別に塗り分ける
      ensureHatch()
      map.addLayer({
        id: `${def.key}-hatch`, type: 'fill', source: srcId(def.key), 'source-layer': def.key,
        filter: ['all', ['==', ['geometry-type'], 'Polygon'],
                 ['==', ['get', 'bui_code'], '']],
        layout: { visibility: visible },
        paint: { 'fill-pattern': 'hatch-unclassified', 'fill-opacity': Math.min(1, alpha + 0.35) },
      })
      map.addLayer({
        id: fillId(def.key), type: 'fill', source: srcId(def.key), 'source-layer': def.key,
        filter: ['all', ['==', ['geometry-type'], 'Polygon'],
                 ['!=', ['get', 'bui_code'], '']],
        layout: { visibility: visible },
        paint: { 'fill-color': buildingColor(theme) as never, 'fill-opacity': alpha },
      })
      // LOD1。高さは実測値がある棟だけ立ち上げる（推定値は作らない）。
      // 施行規則第5条第5号が「建築物の……高さ」を調査項目に挙げており、
      // 東京都 BV_15・さいたま市 TAKASA がその実測値にあたる
      map.addLayer({
        id: extrudeId(def.key), type: 'fill-extrusion',
        source: srcId(def.key), 'source-layer': def.key,
        minzoom: 14,
        filter: ['all', ['==', ['geometry-type'], 'Polygon'], ['has', 'bui_height']],
        layout: { visibility: visible && pitched ? 'visible' : 'none' },
        paint: {
          'fill-extrusion-color': buildingColor(theme) as never,
          'fill-extrusion-height': ['get', 'bui_height'],
          'fill-extrusion-base': 0,
          'fill-extrusion-opacity': 0.9,
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
  map.setStyle(getBasemapStyle(basemap, theme))
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
        ${def.key === 'building' ? buildingLegend() : ''}
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
 * 土地利用の凡例。国標準の用途 20 区分。
 * 識別を色だけに依存させないため、必ず区分名を添える。
 */
function landuseLegend(): string {
  const swatch = (color: string, label: string) =>
    `<li><span class="key" style="background:${color}"></span>${label}</li>`

  const items = CONVENTIONAL_ORDER.map((code) =>
    swatch(CONVENTIONAL[code][theme], CONVENTIONAL[code].name)).join('')

  const hatch =
    `<li><span class="key hatch" style="--hatch:${UNCLASSIFIED[theme]}"></span>` +
    '未分類<span class="legend-note">対照表に記載なし</span></li>'

  return `<ul class="legend conventional">${items}${hatch}</ul>
    <p class="legend-note">東京都土地利用現況図〔建物用途別〕の凡例を参考にした慣行配色。
    区分数が多く、色の分離は検証を通らない（色覚特性によっては区別が難しい
    組み合わせを含む）。判別は凡例の区分名とクリックでの属性表示で補う。</p>`
}


/**
 * 建物の凡例。国標準の建物用途 19 区分。
 * 実データに出ない区分まで並べても読みにくいので、出たものだけ載せる。
 */
function buildingLegend(): string {
  const items = BUILDING_ORDER.map((code) =>
    `<li><span class="key" style="background:${BUILDING[code][theme]}"></span>` +
    `${BUILDING[code].name}</li>`).join('')
  const hatch =
    `<li><span class="key hatch" style="--hatch:${UNCLASSIFIED[theme]}"></span>` +
    '未分類<span class="legend-note">対照表に記載なし</span></li>'
  return `<ul class="legend conventional">${items}${hatch}</ul>
    <p class="legend-note">国土交通省「C0401建物用途コード表」の区分。
    地図を傾けると、実測の高さを持つ棟が立ち上がる（東京都 BV_15・
    さいたま市 TAKASA。階数からの推定はしていない）。</p>`
}

function setVisible(def: ThemeDef, on: boolean): void {
  for (const id of allLayerIds(def.key)) {
    if (!map.getLayer(id)) continue
    // 3D は「テーマが ON」かつ「地図を傾けている」ときだけ出す
    const show = id === extrudeId(def.key) ? on && pitched : on
    map.setLayoutProperty(id, 'visibility', show ? 'visible' : 'none')
  }
}

function setOpacity(def: ThemeDef, v: number): void {
  if (map.getLayer(fillId(def.key))) map.setPaintProperty(fillId(def.key), 'fill-opacity', v)
  const strong = Math.min(1, v + 0.35)
  if (map.getLayer(lineId(def.key))) map.setPaintProperty(lineId(def.key), 'line-opacity', strong)
  if (map.getLayer(pointId(def.key))) map.setPaintProperty(pointId(def.key), 'circle-opacity', strong)
  // 3D は半透明にしない。重なった箱は透かすと形が読めなくなる
}

// ---- 背景地図の切替（右下） ----
class BasemapControl implements maplibregl.IControl {
  private container!: HTMLElement

  onAdd(): HTMLElement {
    this.container = document.createElement('div')
    this.container.className = 'maplibregl-ctrl maplibregl-ctrl-group basemap-ctrl'
    for (const [id, label] of [['pale', '淡色'], ['photo', '写真']] as const) {
      const btn = document.createElement('button')
      btn.type = 'button'
      btn.textContent = label
      btn.title = `背景を${label}に切替`
      btn.className = basemap === id ? 'on' : ''
      btn.addEventListener('click', () => setBasemap(id))
      this.container.appendChild(btn)
    }
    return this.container
  }

  onRemove(): void {
    this.container.remove()
  }
}

const basemapCtrl = new BasemapControl()
map.addControl(basemapCtrl, 'bottom-right')

/** 2D / 3D の切替。傾けると建物が立ち上がる。 */
class PitchControl implements maplibregl.IControl {
  private container!: HTMLElement

  onAdd(): HTMLElement {
    this.container = document.createElement('div')
    this.container.className = 'maplibregl-ctrl maplibregl-ctrl-group basemap-ctrl'
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.textContent = pitched ? '2D' : '3D'
    btn.title = pitched ? '真上から見る' : '傾けて建物を立体表示する'
    btn.addEventListener('click', () => {
      pitched = !pitched
      localStorage.setItem('bskp-pitch', pitched ? 'on' : 'off')
      map.easeTo({ pitch: pitched ? 55 : 0, duration: 400 })
      for (const def of THEMES) if (available.has(def.key)) setVisible(def, active.has(def.key))
      btn.textContent = pitched ? '2D' : '3D'
      btn.title = pitched ? '真上から見る' : '傾けて建物を立体表示する'
    })
    this.container.appendChild(btn)
    return this.container
  }

  onRemove(): void {
    this.container.remove()
  }
}

map.addControl(new PitchControl(), 'bottom-right')

function setBasemap(next: Basemap): void {
  if (next === basemap) return
  basemap = next
  localStorage.setItem('bskp-basemap', basemap)
  // スタイルごと入れ替わるのでデータレイヤを張り直す
  map.setStyle(getBasemapStyle(basemap, theme))
  map.once('styledata', () => {
    addDataLayers()
    buildToggles()
  })
  map.removeControl(basemapCtrl)
  map.addControl(basemapCtrl, 'bottom-right')
}

// ---- クリックで属性表示 ----
map.on('click', (ev) => {
  const ids = interactiveIds()
  if (!ids.length) return
  const hits = map.queryRenderedFeatures(ev.point, { layers: ids })
  if (!hits.length) return

  const f = hits[0]
  const def = themeOf(f.layer.id.replace(/-(fill|line|point)$/, ''))
  // 属性名は配布元のまま（LU_1, BV_15 …）なので、定義書に載っている和名で出す。
  // 原名も併記して、配布元データと突き合わせられるようにしておく
  const rows = Object.entries(f.properties ?? {})
    .filter(([, v]) => v !== null && v !== '')
    .map(([k, v]) => {
      const { label, raw } = labelOf(k)
      const name = raw
        ? `${esc(label)}<span class="field-raw">${esc(raw)}</span>`
        : esc(label)
      return `<tr><th>${name}</th><td>${esc(String(v))}</td></tr>`
    })
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
