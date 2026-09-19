# 出場修正與區間匯出

更新包不含原本 compose.yaml、Ollama URL 或帳本。

## 更新原網頁的匯出功能

將 research-v2-update.tar.gz 傳到 Spark 的 ~/AutoMoney：

```bash
cd ~/AutoMoney
tar -xzf research-v2-update.tar.gz
docker compose up --build -d --no-deps dashboard
```

更新 SOL/XRP 網頁（若已使用該組）：

```bash
ALT_DASHBOARD_BIND=0.0.0.0 docker compose -p automoney-altcoins --env-file config/ollama.env -f compose.yaml -f compose.ai.yaml -f compose.altcoins.yaml up --build -d --no-deps dashboard
```

以上只重建網頁服務，交易程式繼續沿用原本設定。瀏覽器重新整理一次。原來改在 compose.yaml 的8080綁定位址會保留；SOL/XRP指令維持區網8082，網頁無登入，僅在可信任網路使用。

右上角可選開始／結束時間，固定解讀為台灣時間，不受瀏覽器所在時區影響。留空為全部，單邊留空為無該邊界。區間含起訖時間點。ZIP 新增 START_HERE.md 和 summary.json，先看摘要再查完整JSONL。

摘要使用所選區間內首末實際估值，不在整點補造價格；損益不再直接使用開戶本金。opening_observation.json 是起點之前最近一筆估值供核對（若存在），不是插值；manifest 的 account 是匯出當下帳戶，不能當期初餘額。AI呼叫依請求開始時間篩選，決策依完成時間，所以邊界附近兩者筆數可能不同。

匯出只讀資料，不改帳本。檔案含模型回覆與模擬持倉，不含原始提示、設定檔或資料庫檔。

## 啟動 v2 對照實驗

```bash
EXIT_DASHBOARD_BIND=0.0.0.0 bash scripts/research-v2.sh start btc-eth
EXIT_DASHBOARD_BIND=0.0.0.0 bash scripts/research-v2.sh start sol-xrp
```

BTC/ETH新版網頁8083；SOL/XRP新版網頁8084。原8080／8082保持原實驗。Compose需支援!override（2.24.4+）。若只在Spark本機看，省略EXIT_DASHBOARD_BIND設定。

新版只啟動規則策略與網頁，不增加Ollama工作負载。本次沒有更改AI提示或引入商業模型；固定輸入的模型比較仍是後續獨立工作。

每組獨立1000 USDT模擬本金，新版從現金開始；不能把其總損益與已持倉多日的舊組直接當公平績效比較。先觀察出場行為與拒絕原因，再對齊期間及曝險分析。

v2設定 exit_policy_version=2，僅允許PAPER。賣出不要求均線差距大於來回成本，但保留最大執行成本、行情有效、餘額、交易資格、冷卻、每日次數、信心與停止開關。持倉已超額時允許有效減倉，不要求一次賣出立刻降到上限以下。買入規則維持原樣。

規則策略賣出時以全部持倉作目標，上限仍為原單筆額度；不再乘0.99，模擬手續費以USDT扣除。交易所數量步進、最低額度或單筆上限仍可能留下尾數，不保證完全清零。AI若僅提出部分賣出，不會擅自改為全倉賣出。

原v1設定預設保留。舊帳本不允許直接改成v2混跑，啟動腳本以獨立Compose專案及volume隔離。不要down -v。

後續請分別提供舊版與新版的同一時間範圍匯出檔；在START_HERE摘要及manifest可識別幣種和出場版本。
