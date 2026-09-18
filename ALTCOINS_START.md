# SOL／XRP 獨立模擬

新增 SOLUSDT、XRPUSDT，原 BTC／ETH 實驗照常。這是額外 PAPER 測試，不是真實買幣或買入推薦。選擇兩種幣控制第一輪實驗範圍，並非根據一天漲幅判斷未來收益。

將 altcoins-update.tar.gz 放到 Spark 的 ~/AutoMoney，執行：

```bash
cd ~/AutoMoney
tar -xzf altcoins-update.tar.gz
ALT_DASHBOARD_BIND=0.0.0.0 bash scripts/altcoins.sh start
```

在可信任區域網路開啟 http://Spark的IP:8082。8080 仍是原本 BTC／ETH。若只需本機瀏覽，省略 ALT_DASHBOARD_BIND=0.0.0.0，預設僅本機可連。網頁無登入，不要開到公網。此覆蓋設定使用 Compose !override，需要 Docker Compose 2.24.4 或以上。

沿用 config/ollama.env 的 URL／模型，但使用新專案 automoney-altcoins 和獨立 volume。更新包不含你現有的 compose.yaml、config/ollama.env 或帳本。

每個規則帳戶和 Ollama 帳戶各自模擬本金 1000 USDT，SOL／XRP 共用各帳戶限額；不是每種幣再加 1000。仍為無槓桿現貨，總曝險上限 300、單筆上限 100、每日上限 6 次、冷卻 30 分鐘。理論持有基準 70% 現金、SOL／XRP 各 15%。沒有因新增幣種降低成本门檻或要求模型一定買入。

這個本金用來維持與原組相同的實驗設定，不代表你需要投入真錢。原組較早開始，請從新組啟動後的共同時間開始比較。它不是四幣共用同一筆本金的投資組合。

兩個 AI worker 共用同一個 Ollama，可能排隊、延遲和過期，不能把因此產生的差異當作幣種表現。可以先觀察兩邊 AI 呼叫結果、推論時間，必要時另做分時測試。SOL／XRP 的實際交易資格與最小額度由交易所 metadata 檢查；這份更新已用合成行情測試，未在你的 Spark 實際推論。

常用指令：

```bash
bash scripts/altcoins.sh status
bash scripts/altcoins.sh logs
bash scripts/altcoins.sh pause
bash scripts/altcoins.sh resume
bash scripts/altcoins.sh stop
```

重新啟動並維持區網連入時，同樣使用 ALT_DASHBOARD_BIND=0.0.0.0 bash scripts/altcoins.sh start。
在 8080 和 8082 各下載一份分析 ZIP，下載後分別改名為 btc-eth.zip 和 sol-xrp.zip，再提供分析。

不要 down -v，以免刪除模擬資料。若之後換幣種或風控，需另開實驗，不能混用本帳本。
