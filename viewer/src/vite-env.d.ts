/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 本番ビルドでタイルを別ホストに置く場合の URL。未指定なら ./tiles */
  readonly VITE_TILES_BASE?: string
}
interface ImportMeta {
  readonly env: ImportMetaEnv
}
