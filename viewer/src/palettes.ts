/**
 * 土地利用の配色。2 つを切り替えられるようにしてある。
 *
 * ## なぜ 2 つあるか
 *
 * 都市計画の実務で見慣れた慣行配色と、色覚特性への配慮は、この用途数では両立しない。
 *
 * - 慣行配色は 20 区分を塗り分けるため、色の分離を検証すると必ず落ちる。
 *   検証済み 8 色パレットですら全ペア条件で 緑↔橙 CVD ΔE 3.2 で落ちる。
 * - 検証を通せるのは 3 色まで（all-pairs で両モード PASS）。
 *
 * どちらか一方を選ぶのではなく、既定を検証済みにして安全側に倒し、
 * 慣行配色が要る場面では切り替える。
 */

export type PaletteId = 'validated' | 'conventional'
export type Mode = 'light' | 'dark'

// ---- 検証済み（既定） ----

/**
 * 実施要領の大分類に集約した 3 系統。
 * all-pairs 条件で両モード PASS（最悪 CVD ΔE 9.2/9.4、通常視 24.0/20.9）。
 * 4 つ目の未分類は色ではなくハッチで描く。ダークモードでは灰と aqua 系が
 * どの明度でも通常視 ΔE 13.3 前後まで近づき、分離できないため。
 */
export const GROUPS = [
  { value: '自然的土地利用', light: '#1baf7a', dark: '#199e70' },
  { value: '都市的土地利用', light: '#eb6834', dark: '#d95926' },
  { value: '低未利用土地', light: '#2a78d6', dark: '#3987e5' },
] as const

export const UNCLASSIFIED = { light: '#6b7078', dark: '#9aa0a6' } as const
export const GROUP_UNCLASSIFIED = '未分類'

// ---- 慣行配色 ----

/**
 * 国標準の土地利用コード（201-253）に対する慣行配色。
 *
 * 色は「東京都土地利用現況図〔建物用途別〕（区部）（平成28年現在）」の凡例を
 * 参考に定めた。同図は東京都都市整備局が著作権者で無断複製が禁じられているため、
 * 図そのものは複製せず、区分ごとの色調のみを参照している。
 *
 * 元の凡例は建物用途別の 23 区分で、国標準の土地利用 20 区分とは体系が違う。
 * 下の comment は、どの区分を参照したかを残したもの。1 対 1 で対応しない箇所は
 * 最も近い区分を当てている（判断が入っている点は隠さない）。
 *
 * 道路・未利用地は元の凡例では白。地図上で背景と区別がつかないので、
 * ごく薄い灰に置き換えている。
 */
export const CONVENTIONAL: Record<string, { name: string; light: string; dark: string }> = {
  '201': { name: '田', light: '#cfe8ff', dark: '#2d5a80' },            // 田
  '202': { name: '畑', light: '#ffffc7', dark: '#6b6b33' },            // 畑
  '203': { name: '山林', light: '#d6f0dd', dark: '#2f5a3f' },          // 森林
  '204': { name: '水面', light: '#ceeefd', dark: '#2b5570' },          // 水面・河川・水路
  '205': { name: 'その他自然地', light: '#e6dcff', dark: '#4a4166' },   // 原野
  '211': { name: '住宅用地', light: '#7ecfd2', dark: '#2b6f72' },      // 独立住宅
  '212': { name: '商業用地', light: '#e8390c', dark: '#b83214' },      // 専用商業施設
  '213': { name: '工業用地', light: '#0192d5', dark: '#1a6a99' },      // 専用工場
  '214': { name: '公益施設用地', light: '#e7af00', dark: '#9c7700' },  // 教育文化施設（公共系）
  '215': { name: '道路用地', light: '#e0e0e0', dark: '#4a4a4a' },      // 道路（原典は白）
  '216': { name: '交通施設用地', light: '#b5b5b5', dark: '#6e6e6e' },  // 鉄道・港湾等
  '217': { name: '公共空地', light: '#fff0b0', dark: '#6b5f2a' },      // 公園・運動場等
  '218': { name: 'その他の公的施設用地', light: '#a9a194', dark: '#6b665c' }, // 供給処理施設
  '219': { name: '農林漁業施設用地', light: '#77a22e', dark: '#4f6b20' },     // 農林漁業施設
  '220': { name: 'その他の空地（ゴルフ場）', light: '#e8f0c8', dark: '#4f5c33' },
  '221': { name: 'その他の空地（太陽光）', light: '#ede9e0', dark: '#4a4740' },
  '222': { name: 'その他の空地（平面駐車場）', light: '#ffe3e1', dark: '#6b4442' }, // 屋外利用地
  '223': { name: 'その他の空地（その他）', light: '#f0eee8', dark: '#4a4842' },
  '231': { name: '不明', light: '#eeeeee', dark: '#454545' },
  '253': { name: '低未利用土地', light: '#f2eede', dark: '#57533f' },  // 未利用地等
}

/** 慣行配色の凡例に出す順。実施要領の並び（自然的 → 都市的 → 低未利用）に合わせる。 */
export const CONVENTIONAL_ORDER = [
  '201', '202', '203', '204', '205',
  '211', '212', '213', '214', '215', '216', '217', '218', '219',
  '220', '221', '222', '223', '253', '231',
]

/** MapLibre の fill-color 式。palette に応じて lui_group か lui_code で分岐する。 */
export function fillColor(palette: PaletteId, mode: Mode): unknown[] {
  if (palette === 'conventional') {
    const expr: unknown[] = ['match', ['get', 'lui_code']]
    for (const code of CONVENTIONAL_ORDER) expr.push(code, CONVENTIONAL[code][mode])
    expr.push('rgba(0,0,0,0)')   // 未分類はハッチで描くのでここでは塗らない
    return expr
  }
  const expr: unknown[] = ['match', ['get', 'lui_group']]
  for (const g of GROUPS) expr.push(g.value, g[mode])
  expr.push('rgba(0,0,0,0)')
  return expr
}
