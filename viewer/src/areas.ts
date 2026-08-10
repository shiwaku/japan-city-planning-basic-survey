/**
 * 対象地域の表示。
 *
 * このタイルは全国を覆っていない。実際に入っているのは東京都・埼玉県・
 * さいたま市・静岡市・清水町・越谷市・伊奈町・津島市だけで、面積で見れば
 * 国土のごく一部にすぎない。全国を配れているように見えてしまうと、
 * 「自分の街が白い＝土地利用がない」と読まれてしまう。
 *
 * そこで収録範囲を 2 つの形で出す:
 *
 *   パネル  提供元 → 自治体のリスト。名前で絞り込め、選ぶとその範囲へ飛ぶ
 *   地図    引いたときに収録範囲を薄く塗る。どこにデータがあるかが一目で分かる
 *
 * 中身は `bskp tiles` が実データから書く areas.json（手書きにしない）。
 * 範囲はシェープファイルのヘッダにある外接矩形を EPSG:4326 に変換したもので、
 * 自治体の行政界そのものではない。矩形なので海や隣接市を少し含む。
 */

import type maplibregl from 'maplibre-gl'
import type { Theme } from './theme'

export interface Area {
  name: string
  /** その地域に入っている調査項目（土地利用 / 建物） */
  themes: string[]
  features: number
  /** [w, s, e, n]。作れなかった場合は null */
  bbox: [number, number, number, number] | null
}

export interface AreaGroup {
  provider: string
  catalog: string
  url: string
  features: number
  areas: Area[]
}

export const COVERAGE_SRC = 'src-coverage'
export const COVERAGE_FILL = 'coverage-fill'
export const COVERAGE_LINE = 'coverage-line'

export async function fetchAreas(base: string): Promise<AreaGroup[]> {
  const res = await fetch(`${base}/areas.json`)
  if (!res.ok) throw new Error(String(res.status))
  return res.json()
}

const rectangle = (b: [number, number, number, number]): number[][][] =>
  [[[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]], [b[0], b[1]]]]

/**
 * 収録範囲を地図に薄く重ねる。ズームインすると消す——実データが見えている
 * ところに範囲の枠を出しても、区分の塗りを隠すだけで役に立たないため。
 */
export function addCoverageLayer(
  map: maplibregl.Map, groups: AreaGroup[], theme: Theme,
): void {
  const features = groups.flatMap((g) =>
    g.areas.filter((a) => a.bbox).map((a) => ({
      type: 'Feature' as const,
      properties: { name: a.name, provider: g.provider, themes: a.themes.join('・') },
      geometry: { type: 'Polygon' as const, coordinates: rectangle(a.bbox!) },
    })))
  if (!features.length) return

  if (!map.getSource(COVERAGE_SRC)) {
    map.addSource(COVERAGE_SRC, {
      type: 'geojson',
      data: { type: 'FeatureCollection', features },
    })
  }
  const tint = theme === 'dark' ? '#7fb2ff' : '#2a78d6'
  map.addLayer({
    id: COVERAGE_FILL, type: 'fill', source: COVERAGE_SRC,
    paint: {
      'fill-color': tint,
      // 引いているときだけ。z11 で完全に消える
      'fill-opacity': ['interpolate', ['linear'], ['zoom'], 4, 0.16, 9, 0.1, 11, 0],
    },
  })
  map.addLayer({
    id: COVERAGE_LINE, type: 'line', source: COVERAGE_SRC,
    paint: {
      'line-color': tint,
      'line-width': 1,
      'line-dasharray': [3, 2],
      'line-opacity': ['interpolate', ['linear'], ['zoom'], 4, 0.7, 9, 0.45, 11, 0],
    },
  })
}

const esc = (s: string): string =>
  s.replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string)

/**
 * パネルのリスト。提供元ごとに畳み、名前で絞り込めるようにする。
 * 埼玉県だけで 64 市町村あり、並び順（フィーチャ数の多い順）では
 * 自分の街を目で探せないため。
 */
export function renderAreaPanel(
  root: HTMLElement, groups: AreaGroup[], onPick: (area: Area) => void,
): void {
  root.textContent = ''
  const total = groups.reduce((n, g) => n + g.areas.length, 0)

  const head = document.createElement('div')
  head.className = 'field-head'
  head.innerHTML =
    `<span class="field-label">対象地域</span>` +
    `<span class="field-hint">${total}地域 / ${groups.length}提供元</span>`
  root.appendChild(head)

  const note = document.createElement('p')
  note.className = 'area-note'
  note.textContent = '収録しているのはこの範囲だけ。選ぶとその場所へ移動する。'
  root.appendChild(note)

  const filter = document.createElement('input')
  filter.type = 'search'
  filter.className = 'area-filter'
  filter.placeholder = '自治体名で絞り込む'
  filter.setAttribute('aria-label', '自治体名で絞り込む')
  root.appendChild(filter)

  const list = document.createElement('div')
  list.className = 'area-groups'
  root.appendChild(list)

  const empty = document.createElement('p')
  empty.className = 'area-note area-empty'
  empty.textContent = '一致する自治体がありません。'
  empty.hidden = true
  root.appendChild(empty)

  for (const group of groups) {
    const box = document.createElement('details')
    box.className = 'area-group'
    // 地域が少ない提供元は開いたまま。埼玉県の 64 件は畳んでおく
    box.open = group.areas.length <= 4
    box.innerHTML =
      `<summary><span class="area-provider">${esc(group.provider)}</span>` +
      `<span class="area-count">${group.areas.length}地域</span></summary>`

    const ul = document.createElement('ul')
    ul.className = 'area-list'
    for (const area of group.areas) {
      const li = document.createElement('li')
      const btn = document.createElement('button')
      btn.type = 'button'
      btn.className = 'area-item'
      btn.disabled = !area.bbox
      btn.innerHTML =
        `<span class="area-name">${esc(area.name)}</span>` +
        `<span class="area-themes">${esc(area.themes.join('・'))}</span>` +
        `<span class="area-features">${area.features.toLocaleString('ja-JP')}</span>`
      btn.addEventListener('click', () => onPick(area))
      li.appendChild(btn)
      ul.appendChild(li)
    }
    box.appendChild(ul)
    list.appendChild(box)
  }

  filter.addEventListener('input', () => {
    const q = filter.value.trim()
    let shown = 0
    for (const box of list.querySelectorAll<HTMLDetailsElement>('.area-group')) {
      let hit = 0
      for (const li of box.querySelectorAll<HTMLLIElement>('.area-list li')) {
        const name = li.querySelector('.area-name')?.textContent ?? ''
        const on = !q || name.includes(q)
        li.hidden = !on
        if (on) hit++
      }
      box.hidden = hit === 0
      // 絞り込んでいる間は畳まない。開き直す手間をかけさせない
      if (q) box.open = hit > 0
      shown += hit
    }
    empty.hidden = shown > 0
  })
}
