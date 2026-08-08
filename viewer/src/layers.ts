/**
 * 表示テーマの定義と配色。
 *
 * ## 配色の根拠
 *
 * 都市計画基礎調査には、都市計画決定情報における用途地域のような
 * 公表された標準配色が存在しない。そのため色は自前で決める必要があるが、
 * **10 テーマ分のカテゴリカル配色は検証を通せない**。
 *
 * 地図の重ね合わせは「全ペアが同時に画面上で隣接しうる」条件（all-pairs）になる。
 * 検証済み 8 色パレットをこの条件にかけると落ちる:
 *
 *   緑 #008300 ↔ 橙 #eb6834   CVD ΔE 3.2 (protan)   ← 8 以上が目標
 *   赤 #e34948 ↔ 橙 #eb6834   通常視 ΔE 7.1          ← 15 以上が下限（ハードFAIL）
 *
 * 先頭 3 スロットだけなら all-pairs でもライト/ダーク両モードで全項目 PASS する:
 *
 *   最悪ペア CVD ΔE 9.2 (light) / 9.4 (dark)
 *   最悪ペア 通常視 ΔE 24.0 (light) / 20.9 (dark)
 *
 * そこで **同時表示は 3 テーマまで**に制限し、色は 3 スロットから固定で割り当てる。
 * 「色は序列ではなく実体に従う」原則を守るため、割り当てはテーマごとに固定し、
 * ON/OFF で他のテーマの色が変わることはない。4 つ目を選ぼうとした場合は
 * 選択を拒否して、どれかを外すよう促す（勝手に色を作らない）。
 *
 * さらに凡例に必ずテーマ名を出すので、識別は色だけに依存しない。
 */

/** 検証済みカテゴリカル配色の先頭 3 スロット（light / dark）。 */
export const SLOTS = [
  { light: '#2a78d6', dark: '#3987e5' }, // 1 blue
  { light: '#eb6834', dark: '#d95926' }, // 2 orange
  { light: '#1baf7a', dark: '#199e70' }, // 3 aqua
] as const

/** 同時に表示できるテーマ数。配色の検証が通る上限。 */
export const MAX_ACTIVE = SLOTS.length

export interface ThemeDef {
  /** PMTiles のファイル名・source-layer 名と一致する。 */
  key: string
  /** 表示名 */
  name: string
  /** 初期表示 ON/OFF */
  on: boolean
  /** 固定の配色スロット番号（0-2）。ON/OFF では変わらない。 */
  slot: number
  /** パネルに出す説明 */
  desc: string
}

/**
 * 収録テーマ。並び順はパネルの上から順。
 * 調査項目の分類は都市計画基礎調査実施要領の章立て（人口・産業・土地利用・建物・
 * 都市施設・地価・自然環境・公害及び災害・観光景観歴史）に対応させている。
 *
 * slot は 3 つしかないので使い回すが、同時 ON は 3 つまでに制限しているため
 * 同じ色が同時に 2 つ出ることはない（main.ts の canEnable で担保）。
 */
export const THEMES: ThemeDef[] = [
  {
    key: 'landuse', name: '土地利用', on: true, slot: 0,
    desc: '用途別土地利用面積、宅地開発状況、農地・未利用地など。小地域単位のポリゴンで提供されることが多い。',
  },
  {
    key: 'building', name: '建物', on: true, slot: 1,
    desc: '建物利用現況。用途・階数・構造・建築年・耐火構造種別・延床面積など。',
  },
  {
    key: 'population', name: '人口', on: false, slot: 2,
    desc: '人口規模、世帯数、通勤通学流動など。調査区単位で集計される。',
  },
  {
    key: 'industry', name: '産業', on: false, slot: 0,
    desc: '産業分類別の就業人口、事業所数など。',
  },
  {
    key: 'facility', name: '都市施設', on: false, slot: 1,
    desc: '道路、公園、下水道などの都市施設の位置と現況。',
  },
  {
    key: 'landprice', name: '地価', on: false, slot: 2,
    desc: '公示地価、路線価などの地価分布。',
  },
  {
    key: 'nature', name: '自然環境', on: false, slot: 0,
    desc: '緑地、水系、地形など。収録レイヤ数が最も多い。',
  },
  {
    key: 'hazard', name: '災害', on: false, slot: 1,
    desc: '公害及び災害。既往災害の分布、防災拠点・避難場所、浸水想定区域など。',
  },
  {
    key: 'landscape', name: '景観', on: false, slot: 2,
    desc: '観光、景観、歴史。文化財の位置や景観計画区域など。',
  },
  {
    key: 'other', name: 'その他', on: false, slot: 0,
    desc: 'レイヤ名から調査項目を判定できなかったもの。区域界や調査区など共通データを含む。',
  },
]

/**
 * 土地利用の 3 系統別の配色。
 *
 * 国標準の用途は 20 区分あるが、20 色は検証を通せないので実施要領の大分類
 * （自然的／都市的／低未利用）に束ねる。3 色なら all-pairs でも両モード PASS する。
 *
 * 4 つ目の「未分類」に中立色を当てる案は検証で落ちた。ダークモードでは灰と
 * aqua 系がどの明度でも 通常視 ΔE 13.3 前後まで近づき分離できない
 * （明度帯 L 0.48-0.67 が狭いため構造的に回避できない）。
 * そこで未分類は色ではなくハッチ（テクスチャ）で描く。色以外の手掛かりなので
 * 色覚特性にも印刷にも依存しない。
 */
export const LANDUSE_GROUPS = [
  { value: '自然的土地利用', light: '#1baf7a', dark: '#199e70' },
  { value: '都市的土地利用', light: '#eb6834', dark: '#d95926' },
  { value: '低未利用土地', light: '#2a78d6', dark: '#3987e5' },
] as const

/** 未分類のハッチの線色。塗りつぶしの色ではないので配色スロットを消費しない。 */
export const UNCLASSIFIED = { light: '#6b7078', dark: '#9aa0a6' } as const
export const GROUP_UNCLASSIFIED = '未分類'

/** MapLibre の fill-color 用。lui_group で分岐し、該当なしは透明にする
 *  （未分類は別レイヤでハッチを敷くため、ここでは塗らない）。 */
export function landuseColorExpression(mode: 'light' | 'dark'): unknown[] {
  const expr: unknown[] = ['match', ['get', 'lui_group']]
  for (const g of LANDUSE_GROUPS) expr.push(g.value, g[mode])
  expr.push('rgba(0,0,0,0)')
  return expr
}

export const themeOf = (key: string): ThemeDef | undefined =>
  THEMES.find((t) => t.key === key)

export const colorOf = (def: ThemeDef, mode: 'light' | 'dark'): string =>
  SLOTS[def.slot][mode]
