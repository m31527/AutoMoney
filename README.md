# Crypto 投資工具 — v0.2 PAPER 比較版

依照 `AI_Crypto_Trading_MVP_Codex.md` 分階段開發，目前已完成 Foundation、交易所唯讀介面、確定性風控，**Phase D baseline 策略與 PAPER 模擬交易管線**，以及 **Phase E AI 策略介面**。本專案是現貨交易實驗，沒有獲利保證。PAPER 使用正式市場公開行情，在本機模擬成交；交易所網路下單／取消仍封鎖。AI 為可選策略，預設仍是 HOLD。`resume` 只解除全域停止旗標，不解除當日已觸發的日損限制。

## 搬機、自訂 Ollama URL 與匯出

請看 [簡易安裝說明](OLLAMA_START.md)：填入 `config/ollama.env` 的 URL 與模型名稱，執行 `bash scripts/ai.sh start`，同時啟動原本規則組與 Ollama 模擬組。網頁右上角「匯出分析資料」可下載 ZIP，供後續比較。

## 在 DGX Spark 跑本地 Qwen 模型

已加入可選的 Ollama `qwen3.8:27b` 獨立 PAPER 帳戶與 NVIDIA GPU Compose 設定。請依 [Spark 啟動說明](SPARK_START.md) 操作；不需要 API key，原本預設 Compose 不會自動啟用 Ollama。這是模型推論接入，尚未加入自行訓練或自動修正策略的流程。

## 看結果：直接打開儀表板

Docker 啟動後，在這台電腦的瀏覽器開啟 **[http://localhost:8080](http://localhost:8080)** 即可。

頁面每 30 秒更新，顯示兩組策略與持有／現金基準的淨值、損益、最大回落、費用及曲線。下方可切換策略或舊版帳本，篩選成交／等待／拒絕，並翻頁查看全部已保存的決策紀錄、成交價格與數量、行情錯誤與共用控制帳本的系統事件。不包含 Docker 自身的啟停或尚未保存的程式崩潰日誌。

```bash
# 啟動或更新交易服務與儀表板
docker compose up --build -d
# 只啟動或更新儀表板，不重啟交易服務
docker compose up --build -d dashboard
```

頁面與掛載的資料 volume 均為唯讀，只開放這台電腦的 `127.0.0.1:8080`。關掉網頁不會停止模擬交易。超過 15 分鐘沒有新觀測會提示延遲；「近期資料已更新」不是即時程序存活檢查。

首次尚無資料時會顯示等待訊息；曲線最多取樣 600 點，最大回落使用全部觀測計算。表格每頁 30 筆，可翻查歷史。頁面沒有下單、停止或重設帳戶按鈕，既有 `kill`／`resume` 指令仍有效。

## Docker v2：雙幣、多策略比較（預設）

啟動 Docker Desktop，在專案目錄執行：

```bash
docker compose up --build -d
# 平時只需這一行，查看中文報告：
docker compose run --rm trader report
```

背景程式每五分鐘取得同一批 BTC／ETH 公開行情，並運行兩個**彼此獨立**的模擬帳戶：

| 組別 | 本金 | 規則 |
| --- | --- | --- |
| 5 分鐘 SMA | US$1,000 | 保留 SMA(5/20) 交叉策略，兩幣共用資金 |
| 1 小時趨勢 | US$1,000 | 每根已收盤小時 K 線評估；快均線高於慢均線且持倉市值低於 US$5 才提出買入，反向且持倉至少 US$5 時提出賣出（避免零碎餘額永久阻止再進場） |

每組保留單筆 US$100、總曝險 US$300、日損 US$20、每日 6 筆、30 分鐘冷卻等限制；策略提出約 US$50 的訂單，沒有為增加成交而調低成本門檻。同組兩幣共用所有限制；兩組不是共同管理一筆 US$2,000 真實資金。5 分鐘組每半小時、小時組每小時輪替先評估的幣種，避免永遠讓 BTC 優先。

同一根 K 線只評估一次，重啟不會重複下模擬單。小時組會等下一根已收盤小時 K 線才再次評估，期間仍更新淨值。它同時改變週期與進出場規則，因此不能將結果差異全部歸因於週期。MA 差距仍是未校準的實驗代理值，不是預期收益。

報告包含淨值、扣已付費用後損益、報酬率、觀測到的最大回落、成交／等待／拒絕次數、最近拒絕的訊號與成本數值，以及行情錯誤與資料過期提示。HOLD 不會再在中文摘要中顯示成一串風控錯誤。報酬不年化，未賣出資產不預扣退出費用，最大回落按約五分鐘的淨值樣本估算。

基準於首次有效行情固定：全現金，以及 **70% 現金、BTC／ETH 各投入 15%** 的持有組合。持有基準含買入費用、價差和滑價，固定數量、不再平衡；它是可分割數量的理論指數，不經交易所最小單量過濾，也不是真實訂單。它和策略具有不同的實際持倉比例；報告的相對損益不是風險調整後 alpha。

```bash
# 查看近期中文運行日誌
docker compose logs --tail 30 trader
# 暫停兩組的後續交易（行情與基準仍更新）
docker compose run --rm trader kill
# 恢復；不解除日損鎖定
docker compose run --rm trader resume
# 停止整個服務，保留帳本
docker compose down
```

Docker Desktop 與電腦需保持運行。行情 API 失敗時本輪不交易，記錄錯誤並五分鐘後重試；儲存損壞、設定衝突等非行情錯誤會停止服務，不自動重啟。

資料保存在既有 `paper-data` volume，**不要執行 `down -v`**：

- `/data/paper.db`：舊版帳本保留，同時作為共用 kill/resume 控制；`paper-status` 查詢的是這份舊帳本。
- `/data/experiment-v2/`：新版兩組帳本、同期基準、觀測與錯誤紀錄；請使用 `report` 查看。

新版實驗的策略版本與風控設定會固定保存。修改 `config/default.toml` 後不會將新設定混入舊實驗，程式會拒絕啟動；新參數應使用新的實驗資料目錄與起始時間。舊版不會自動合併到新版績效中。

Compose 固定 PAPER 且停用 LIVE，預設比較不需要金鑰，也不會呼叫 AI。`PAPER_STRATEGY`／`PAPER_SYMBOL` 不影響比較模式；`PAPER_CYCLES=1` 可限制比較執行一輪，預設 0 持續運行。舊版單策略指令仍保留，先停止比較服務後才另外執行 `docker compose run --rm trader paper-start`；下方 AI 設定僅適用於單策略模式。

## 安裝與操作

需要 Python 3.12+。執行時僅依賴 `certifi` 提供 CA 憑證，避免本機 Python 未配置根憑證時無法驗證 HTTPS。TLS 驗證始終開啟。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m trader --config config/default.toml status
python -m trader kill
python -m trader status
python -m trader resume
python -m trader market --symbol BTCUSDT --limit 100
python -m trader market --symbol ETHUSDT --limit 100
```

stdout 為 JSON 狀態，stderr 為 JSON 日誌；錯誤回傳 exit code 1。所有程序必須使用同一個 `DATABASE_URL`，才能共享停止狀態。預設資料庫為目前工作目錄下的 `data/trading.db`；部署時建議指定絕對路徑，例如 `sqlite:////absolute/path/trading.db`。

`market` 不需 API 金鑰，查詢 ticker、交易規則、1m／5m／15m／1h K 線，並寫入公開行情快照。最新 K 線可能尚未收盤。公開行情觀察不捏造帳戶資料；PAPER 管線會另外整合本機帳本，組合完整的策略 `MarketSnapshot`。

使用正式市場的公開行情可執行：

```bash
TRADING_MODE=PAPER python -m trader market --symbol BTCUSDT --limit 100
```

Testnet HMAC 金鑰由部署環境提供 `BINANCE_API_KEY`、`BINANCE_API_SECRET` 後，可執行 `python -m trader account`。金鑰不能寫在命令列參數；只在此指令載入到記憶體，不保存於 DB。帳戶輸出包含 free／locked 餘額；`key_permissions_verified=false` 明確表示尚未驗證金鑰權限。`canWithdraw` 為交易所帳戶欄位，不能用來宣稱 API 金鑰已關閉提款。

## PAPER 模擬交易

完全離線、無憑證的合成資料示範（臨時 DB，自動清理）：

```bash
python examples/paper_trading.py
```

會依序執行 SMA 上穿 BUY、平盤 HOLD、下穿 SELL，輸出成本後權益及損益。這是管線示範，不是回測績效。

使用即時公開行情，在專用 PAPER DB 初始化一次：

```bash
export TRADING_MODE=PAPER
export DATABASE_URL=sqlite:///data/paper.db
python -m trader paper-init
python -m trader paper-run --strategy hold --cycles 1
python -m trader paper-run --strategy sma --symbol BTCUSDT --cycles 1
python -m trader paper-status
```

`paper-init` 只在第一次建立設定中的模擬本金，重跑不會重設持倉、交易次數或日損；具有其他交易／風控紀錄的 DB 會拒絕初始化。`paper-status` 的最後估值附時間戳，並非即時行情。

`--cycles 0` 可持續執行，每輪間隔五分鐘；Ctrl-C 安全停止。`--symbol ETHUSDT` 可評估 ETH。每輪取得白名單所有資產行情，以便估值現有持倉，但僅對指定 symbol 提案。任何網路／儲存錯誤會結束程序；網路層已有有限重試，不會忽略錯誤繼續成交。此版本不提供常駐 daemon 或自動重啟部署。

- `hold` 是預設 no-op 策略。`sma` 僅用已收盤的連續 5m K 線，採 SMA(5/20) 交叉，預設 US$50 提案；沒有交叉、歷史不足或沒有可賣持倉時 HOLD。MA 差距只是未校準的策略優勢估計，不能當作報酬預測。
- 每輪取得帳本及最新行情，風控、交易規則檢查、模擬成交、持倉與成本基礎更新、成交計數和稽核鏈，在同一筆 `BEGIN IMMEDIATE` 交易內完成。並行程序共享 DB 時會序列化檢查；同一 cycle ID 不會重複成交。
- 模擬數量向下捨入，同時滿足 LOT_SIZE 與 MARKET_LOT_SIZE 的步進；檢查適用於 MARKET 的 MIN_NOTIONAL／NOTIONAL。需要均價的規則使用 `/avgPrice` 且驗證其分鐘數及新鮮度，不以最新價冒充五分鐘均價。這是本機支援的 MARKET filter 子集，尚不代表完整真實交易所准入驗證。
- BUY 以較保守的 ask／last 加設定滑價成交，SELL 以 bid／last 減滑價成交；費用以 USDT 扣除。買入費用納入成本基礎，賣出計算扣費的已實現損益；滑價已反映於成交價格，不重複扣除。每個模擬 MARKET 一次完全成交，不模擬深度、延遲或部分成交。
- UTC 換日後，持倉以該日 00:00 的 1h K 線開盤價建立日初權益；缺少可靠開盤資料時拒絕交易。模擬帳本沒有外部入出金功能，因此不會混入現金流。
- `kill`、`resume` 必須使用相同 `DATABASE_URL`。停止狀態下仍可觀察與保存拒絕原因，但不能產生成交。

## 設定與安全

- `.env.example` 僅提供環境變數範本；程式不自動讀取 `.env`。請由 shell 或部署環境設定。
- `TRADING_MODE` 預設 `TESTNET`，只接受 `PAPER / TESTNET / LIVE`。
- `LIVE` 需要精確的 `ENABLE_LIVE_TRADING=true`，但本階段即使設定也沒有交易功能。此旗標不代表通過規格中的 LIVE 人工審查。
- 風控參數使用 `--config` 指定的 TOML；不指定則使用同一組內建預設值。採 TOML 是為了使用 Python 標準函式庫並避免額外 YAML 相依。
- 設定拒絕未知欄位、非有限數字、非法範圍，以及開啟槓桿、放空、提款。
- kill 狀態與事件以同一筆 SQLite 交易提交；每次檢查都讀取持久狀態。程序內也有停止旗標，寫入失敗時仍停止該程序。儲存故障必須由未來執行引擎視為拒絕執行。
- 日誌只輸出程式定義的事件名稱，不輸出設定值、環境、例外內容或憑證。
- 預設 HTTP transport 只允許 GET，禁止重導向，網址固定為 Testnet 或 PAPER 公開行情服務。沒有能啟用網路交易的環境旗標；LIVE adapter 仍未提供。
- GET 暫時性錯誤最多嘗試三次，使用指數退避；遵守 `Retry-After`，較長等待直接回傳限流錯誤，同一 adapter 在期限內不再發送。PAPER 迴圈遇到錯誤會退出，請勿密集重開程序繞過等待。
- 認證錯誤、異常 metadata、非 SPOT 帳戶、對帳失敗會觸發持久化停止與 CRITICAL 事件；連續三次用盡重試的網路查詢失敗亦會停止。監控查詢仍可使用。

## 架構

- `config.py`：不可變設定模型、Decimal 金額、環境與 TOML 驗證。
- `models.py`：行情、K 線、策略提案、風控結果、持倉與投資組合型別。
- `storage/db.py`：SQLite schema v4、外鍵、WAL、完整同步寫入。舊版本自動升級，保留停止狀態與歷史紀錄。
- `storage/repository.py`：狀態與事件原子寫入、行情和投資組合快照；Decimal 以文字保存精度，時間使用 UTC。
- `safety/kill_switch.py`：程序內與跨重啟停止控制。
- `main.py` / `logging.py`：CLI 與結構化日誌。
- `exchange/base.py`：交易所協定；`exchange/binance.py`：Testnet adapter、HMAC-SHA256 簽章、伺服器時間校正、有限重試與對帳。
- `exchange/models.py` / `parsing.py`：Decimal 型別化的餘額、價格、交易規則、訂單、成交；拒絕缺漏或不合法的回應。
- `exchange/transport.py`：可注入 mock 的 HTTP 邊界；出貨的網路實作維持唯讀。
- `market/data.py`：四種 K 線週期與行情整合、時效檢查。
- `storage/exchange_journal.py`：提交紀錄、追加式訂單觀察與成交去重。
- `portfolio/valuation.py`：由 free／locked 餘額與行情計算目前權益及曝險，拒絕無法估值的非零資產。
- `risk/engine.py`：無網路副作用的確定性風控，逐條回傳通過／拒絕結果及數量上限。
- `risk/service.py`：UTC 日損鎖定、每日成交計數／冷卻與不可覆寫的評估稽核。
- `strategy/baseline.py` / `market/indicators.py`：HOLD 與確定性 SMA baseline。
- `execution/paper.py` / `execution/filters.py`：本機成交、Decimal 帳本與交易規則檢查。
- `storage/transaction.py`：巢狀 savepoint，確保風控子操作不會提前提交整筆模擬交易。
- `scheduler.py`：五分鐘 PAPER 評估迴圈。

PAPER 已串接 market snapshots → AI decisions → risk decisions → orders → fills 的外鍵鏈；沿用 `ai_decisions` 資料表存策略提案，`raw_response.provider=deterministic_baseline` 明確區分非 AI 決策。`paper_account / paper_positions / paper_cycles` 保存模擬帳本及冪等紀錄。Phase B 的 `exchange_submissions / exchange_observations / exchange_fills` 是 adapter 層的獨立對帳紀錄，尚未接入真實風控執行。

訂單路徑僅由離線 mock transport 測試：先以唯一 client order ID 原子寫入請求，再提交一次 MARKET。相同 ID／相同請求只查詢；不同請求使用相同 ID 則拒絕。逾時、5xx 或 Binance 不明執行狀態必須查詢，不會重送；查不到時停止，連操作人員 resume 也不會自動重送該 ID。請勿刪除提交紀錄來「修復」不明訂單。

對帳另外查詢 `/myTrades` 取得成交和費用，支援分頁與部分成交。同一成交重複出現不重複新增；資料衝突不覆寫。成交量或成交金額未完整對齊時，保存 `fills_complete=false` 並停止；保留原始 fee asset 與金額，尚未換算其他資產的 USDT 費用或計算 PnL。每個交易環境／帳戶應使用獨立 DB；Testnet 重置後保留舊 DB 稽核，人工建立新實驗，禁止自行清空 ID 紀錄。

## Phase C 風控

`RiskService.assess` 接收策略提案和由可信帳戶／策略服務提供的 `RiskContext`，不接受 AI 自報權益或交易次數。評估本身不會下單。

- 每單 US$100、總曝險 US$300、單資產配置 20%、每天 6 筆、成交後 30 分鐘、最低信心 0.65，均使用設定值。配置上限依目前權益並預留估計交易成本計算。
- 同時檢查白名單、SPOT／無槓桿／無放空／無提款、資料時效、帳戶與金鑰確認、完整歷史、未結訂單、餘額、費用及滑價。持倉鎖定餘額計入曝險，但不能拿來支付或出售。
- 行情與帳戶取得時間最多 60 秒，未來時間也拒絕；帳戶 `updateTime` 不代表查詢時間。未知非零資產（例如 BNB）在具備可靠估值支援前拒絕核准。
- 日損採 **UTC**：目前權益減去經驗證的 UTC 開盤權益，反映當日已實現與未實現價值變化及已扣除費用。前提是帳戶與帳本已對齊且沒有外部資金流；若有入金、出金或 Testnet 重置，必須標記 `account_reconciled=false`，不得直接拿權益差當交易損益。
- `initialize_day` 只能由可信帳本／操作端提供可追溯的開盤基準，不會用午間的第一次觀察冒充午夜權益；基準不可覆寫。新一天缺少基準會拒絕交易。
- 日損達 US$20，SQLite 原子保存 `DAILY_LOSS_LIMIT_TRIGGERED` 與當天 BUY 鎖定；重啟、價格回升及 `resume` 均不清除。依規格較具體的日損要求，SELL 仍可接受其他規則檢查，系統不會自動清倉。
- 交易次數只由確認成交紀錄計算，同一訂單只計一次；HOLD 與預先核准不計數。部分成交更新最後成交時間，冷卻依最新成交計算，跨 UTC 日期仍保留冷卻。
- 預設單邊費率及滑價各為 0.1%；預估來回成本包含買賣價差、兩邊滑價與費用，上限 50 bps，策略提供的確定性預期優勢扣成本後至少 5 bps。這些是可配置估計，不能代表實際交易費率或保證成交價格。
- AI `invalid_if` 的自由文字目前無法確定性驗證，因此非空時拒絕，避免默默忽略；目前尚未提供可執行的條件語言。

`risk_days / risk_assessments / risk_executions` 保存基準、完整輸入／設定／規則结果與成交統計。下單前仍需由 Execution Engine 重新檢查最新狀態，處理量化捨入、交易所 filters、資金預留及併發；`maximum_quantity` 是未按交易所步進捨入的保守上限，**不是可直接提交的訂單或執行授權**。目前網路 transport 的寫入封鎖仍在。

可執行無網路、無憑證的示範（使用臨時 DB）：

```bash
python examples/risk_preflight.py
```

依序顯示 US$100 BUY 通過、US$101 拒絕、日損 US$20 拒絕，以及權益回升後仍拒絕。

## 可選 AI 策略（Phase E）

先使用 PAPER 專用資料庫並執行 `paper-init`。在 shell 設定 `AI_PROVIDER=openai`、`AI_MODEL`（明確選擇支援 Responses Structured Outputs 的模型）與 `AI_API_KEY`，再執行：

```bash
python -m trader paper-run --strategy ai --cycles 1
```

`.env` 不會自動載入。HOLD／SMA 不需要 AI 設定。選擇 AI 後會向 OpenAI 發送已收盤行情、模擬持倉、現金與損益上下文，可能產生 API 費用；不傳送 Binance 憑證。請勿把金鑰放進命令列參數。請求使用 `store=false`、固定 HTTPS endpoint、30 秒逾時、無工具權限、不自動重試。

- 嚴格 JSON schema 加上本機驗證：拒絕額外欄位、重複鍵、非有限數字、錯誤交易對及不合法型別。模型僅提出名目金額，最終數量由風控計算。
- 格式錯誤、拒答或 provider 失敗改為 HOLD；額度、成本、日損、行情時效及 kill switch 仍由確定性風控判斷。自由文字 `invalid_if` 非空時拒絕交易。
- AI 呼叫不佔用資料庫交易；返回後重新比對帳本上下文，再於原子交易內評估風控及模擬成交。等待期間帳本改變則 HOLD，行情逾時則拒絕。
- `ai_calls` 保存提示、回覆或失敗代碼，決策 audit 記錄對應 call ID；已設定的 API key 會遮罩。資料庫含完整模擬帳戶上下文，請妥善保管。
- 成本門檻使用本機 SMA 差距作為未校準的實驗性 edge proxy，AI 無法覆寫；這不是預期收益估計。

測試使用 mock provider，尚未以真實 OpenAI 模型驗證端到端呼叫。

## 驗證

```bash
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
mypy
```

測試不需要憑證或網路。涵蓋設定、跨程序 kill/resume、v1 升級、簽章與時鐘校正、BTC／ETH 行情與 filters、429 退避、認證失敗、逾時對帳、跨重啟及並行 ID 去重、部分成交、成交缺漏／衝突、手續費保存、網路寫入封鎖及 CLI 行情保存。

## 官方介面參考

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)：Responses `text.format` 嚴格 JSON schema 與拒答處理。

- [Binance Spot REST API](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md)：簽章、行情、帳戶、order／openOrders／myTrades 與錯誤語義。
- [Spot Testnet](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md)：Testnet 主機與定期重置。
- [Symbol filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md)：PAPER 已實作 LOT_SIZE／MARKET_LOT_SIZE／MIN_NOTIONAL／NOTIONAL 的 MARKET 子集；真實執行仍需完整交易所檢查。

## 下一階段

接著補完整績效／同期基準報告，以及 Phase F Testnet burn-in 所需的執行與監控能力。Testnet 私有帳戶、權限驗證與網路執行尚未完成，兩週 burn-in 與 LIVE 人工檢查尚未進行。
