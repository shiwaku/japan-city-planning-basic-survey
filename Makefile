# 都市計画基礎調査データの収集から地図表示まで
.PHONY: help harvest scrape fetch extract convert tiles viewer dev clean

help:
	@echo "make harvest   CKANカタログ横断でインベントリを作る"
	@echo "make scrape    非CKANサイトを走査する"
	@echo "make fetch     実ファイルを取得する（大きいので時間がかかる）"
	@echo "make extract   書庫を展開する"
	@echo "make convert   EPSG:4326のGeoJSONSeqに変換する"
	@echo "make tiles     調査項目ごとにPMTilesを作る"
	@echo "make dev       ビューアを開発モードで起動する"
	@echo "make viewer    ビューアをビルドしてタイルを配置する"

BSKP = PYTHONPATH=src python3 -m bskp

harvest: ; $(BSKP) harvest --limit 0
scrape:  ; $(BSKP) scrape
fetch:   ; $(BSKP) fetch
extract: ; $(BSKP) extract
convert: ; $(BSKP) convert
tiles:   ; $(BSKP) tiles

dev:
	cd viewer && npm install && npm run dev

viewer:
	cd viewer && npm install && npm run build
	rm -rf viewer/dist/tiles
	cp -r data/tiles viewer/dist/tiles
	@echo "viewer/dist/ を任意の静的ホスティングに置いてください"

clean:
	rm -rf data/work data/processed data/tiles viewer/dist
