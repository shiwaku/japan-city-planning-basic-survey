/**
 * 土地利用の配色。国標準の用途コード（201-253）を 20 区分で塗り分ける。
 *
 * ## 3 系統への集約はやめた
 *
 * 以前は「自然的／都市的／低未利用」の 3 色を既定にしていた。20 色の
 * カテゴリカル配色は色の分離検証を通せない、という理由だった。
 *
 * これは検証の当てどころを間違えていた。all-pairs の ΔE 検証は、重ね合わせた
 * テーマを**色だけで**見分けるための条件で、単一レイヤ内の用途区分には、
 * 凡例・空間パターン・クリックでの属性表示・慣行（田は水色、商業は赤）が効く。
 *
 * 実害のほうが大きかった。実測で都市的土地利用が 75.9%（2,106,459/2,776,003）を
 * 占めるため、市街地を見にいくと画面の 4 分の 3 が同じ色になり、
 * 「住宅か商業か工場か」という肝心の情報が消えていた。
 */

export type Mode = 'light' | 'dark'

/** 未分類（対照表に記載がなく写せなかったもの）のハッチ色。 */
export const UNCLASSIFIED = { light: '#6b7078', dark: '#9aa0a6' } as const

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

/** MapLibre の fill-color 式。lui_code で 20 区分に塗り分ける。 */
export function fillColor(mode: Mode): unknown[] {
  const expr: unknown[] = ['match', ['get', 'lui_code']]
  for (const code of CONVENTIONAL_ORDER) expr.push(code, CONVENTIONAL[code][mode])
  expr.push('rgba(0,0,0,0)')   // 未分類はハッチで描くのでここでは塗らない
  return expr
}

// ---- 建物用途 ----

/**
 * 国標準の建物用途コード（401-471）の配色。
 *
 * 土地利用と同じ「東京都土地利用現況図〔建物用途別〕（区部）」の凡例の色調を
 * 参照している（図そのものは複製しない）。土地利用側と系統をそろえた:
 *
 *   住宅系   青緑   商業・業務系 赤橙   公共系 黄
 *   工業系   青     運輸倉庫    青灰   農林漁業 緑
 *
 * 併用住宅は「住宅×商業」「住宅×工業」なので、両者の中間色を当てている。
 * 空家は法定の調査項目（施行規則第5条第9号）なので独立した色を持たせる。
 */
export const BUILDING: Record<string, { name: string; light: string; dark: string }> = {
  '401': { name: '業務施設', light: '#f07f3c', dark: '#b85f28' },
  '402': { name: '商業施設', light: '#e8390c', dark: '#b83214' },
  '403': { name: '宿泊施設', light: '#d1568f', dark: '#9c3f6b' },
  '404': { name: '商業系用途複合施設', light: '#f2a25e', dark: '#ad7340' },
  '411': { name: '住宅', light: '#7ecfd2', dark: '#2b6f72' },
  '412': { name: '共同住宅', light: '#3aa9ad', dark: '#1f7a7d' },
  '413': { name: '店舗等併用住宅', light: '#e8a48f', dark: '#8f5a49' },
  '414': { name: '店舗等併用共同住宅', light: '#d98a6e', dark: '#7d4a37' },
  '415': { name: '作業所併用住宅', light: '#b9a3d1', dark: '#5f5175' },
  '421': { name: '官公庁施設', light: '#e7af00', dark: '#9c7700' },
  '422': { name: '文教厚生施設', light: '#f2d35a', dark: '#8f7a2a' },
  '431': { name: '運輸倉庫施設', light: '#8fa8c8', dark: '#4a5f7d' },
  '441': { name: '工場', light: '#0192d5', dark: '#1a6a99' },
  '451': { name: '農林漁業用施設', light: '#77a22e', dark: '#4f6b20' },
  '452': { name: '供給処理施設', light: '#a9a194', dark: '#6b665c' },
  '453': { name: '防衛施設', light: '#6b7f6b', dark: '#44523f' },
  '454': { name: 'その他', light: '#cfcabf', dark: '#55524b' },
  '461': { name: '不明', light: '#bdbdbd', dark: '#4f4f4f' },
  '471': { name: '空家', light: '#efe3b8', dark: '#6b6040' },
}

/** 凡例の並び。コード表の順（業務・商業 → 住宅 → 公共 → 産業 → その他）。 */
export const BUILDING_ORDER = [
  '401', '402', '403', '404',
  '411', '412', '413', '414', '415',
  '421', '422', '431', '441', '451', '452', '453',
  '454', '461', '471',
]

/** MapLibre の fill-color 式。bui_code で塗り分ける。 */
export function buildingColor(mode: Mode): unknown[] {
  const expr: unknown[] = ['match', ['get', 'bui_code']]
  for (const code of BUILDING_ORDER) expr.push(code, BUILDING[code][mode])
  expr.push('rgba(0,0,0,0)')   // 未分類はハッチで描く
  return expr
}
