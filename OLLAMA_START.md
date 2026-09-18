# 搬到另一台電腦：規則組與 Ollama 同時模擬

## 1. 搬檔案

把 `artifacts/auto-money-spark.tar.gz` 傳到 DGX Spark，解壓縮：

```bash
tar -xzf auto-money-spark.tar.gz
cd auto-money-spark
```

這是程式安裝包，不包含 Mac 上的帳本、密鑰或模型。另一台會以各組 $1000 模擬本金開始。

## 2. 選一種啟動方式

### 已經有 Ollama：設定 URL

先確認另一台已安裝 Docker Compose，而且 Ollama 已下載你要使用的模型。設定檔操作：

```bash
cp config/ollama.env.example config/ollama.env
nano config/ollama.env
```

改成你的連線位置與 `ollama list` 顯示的完整模型名稱，例如：

```dotenv
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=qwen3.8:27b
OLLAMA_TIMEOUT_SECONDS=120
PAPER_CYCLES=0
```

- Ollama 跑在同一台主機：使用上面的 `host.docker.internal`。Docker 裡的 `localhost` 是容器自己。
- Ollama 跑在其他主機：填入例如 `http://192.168.1.100:11434`，換成實際 IP。
- URL 只填主機與連接埠，不要加 `/api/chat`。
- 主機版 Ollama 必須監聽容器可連到的介面。若只監聽 `127.0.0.1`，需在 Ollama 服務環境設定 `OLLAMA_HOST=0.0.0.0:11434` 並重啟該服務；只允許可信任的主機／Docker 網路連入，不要開放到公網。

接著啟動：

```bash
bash scripts/ai.sh start
```

它會同時啟動規則組、Ollama 模擬組、網頁。這個方式使用你指定的 Ollama，不會替它下載模型或啟動模型伺服器。

### 還沒有 Ollama：Spark 全 Docker 安裝

```bash
bash scripts/spark.sh setup
```

這會額外啟動 GPU Ollama 容器並下載模型。詳細硬體前置條件見 [SPARK_START.md](SPARK_START.md)。兩種方式擇一即可。

## 3. 看結果、下載給我

在新電腦的瀏覽器開啟 **http://localhost:8080**。

- 原本程式：5 分鐘 SMA、1 小時趨勢，另外保留持有與現金基準。
- Ollama：獨立帳戶，模型提出建議，既有風控判斷是否執行。
- 在「紀錄來源」選 Ollama AI 決策／AI 呼叫結果，即可確認模型是否成功運行。
- 按右上方 **「匯出分析資料」**，下載 ZIP，再把 ZIP 傳給我。

ZIP 包含各組完整決策、成交、淨值、風控原因、模型回覆和錯誤；也有模型名稱、風控參數與重疊觀測時間。匯出只讀資料，不會暫停或重置交易。初始沒有資料時仍可下載，清單會標記缺少的帳本。

分析時會對齊時間，再比較報酬、回落、成本、曝險、等待／拒絕與模型錯誤；模型推論花費時間，兩組不保證相同成交時點。這是模型推論實驗，沒有自動訓練或改策略；AI 仍受原本 SMA 訊號代理值和成本門檻限制。

若從 Mac 看 Spark，使用你已有的 SSH 帳號：

```bash
ssh -N -L 8081:127.0.0.1:8080 你的帳號@你的Spark主機
```

然後開啟 http://localhost:8081，再下載 ZIP。

## 平常只需這些指令（自訂 URL 方式）

```bash
bash scripts/ai.sh status  # 看服務狀態
bash scripts/ai.sh logs    # 看模型錯誤
bash scripts/ai.sh stop    # 停止，資料保留
bash scripts/ai.sh start   # 重新啟動／套用 URL 設定
```

修改 URL 可以沿用帳本；請確保新端點提供的是同一模型。修改模型或風控需開始新實驗，程式會拒絕混用。不要執行 `docker compose down -v`，會刪除資料。

此版已測試程式與模擬匯出；尚未連到你的 Spark 實測 GPU 推論。若沒有成交，先看 AI 呼叫錯誤及風控拒絕原因，而非直接降低風控。
