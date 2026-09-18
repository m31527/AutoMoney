# 在 DGX Spark 128GB 啟動 Qwen 本地 AI 實驗

此版本支援 `qwen3.8:27b`，透過 Ollama 本地推論提出 BTC／ETH 的買入、賣出或等待建議。不需要 OpenAI／Binance API key，不會下真實訂單。推論不等於自行訓練或自動改寫策略，收益不保證。

已有 Ollama 且想自訂 URL，請改用 [自訂 URL 安裝說明](OLLAMA_START.md)。兩種啟動方式都可在網頁右上角「匯出分析資料」下載 ZIP。

## 第一次使用：只需要一個啟動指令

1. 把 `auto-money-spark.tar.gz` 搬到 Spark，例如用 USB 或你原本的檔案傳輸方式。
2. 在 Spark 的終端機執行：

```bash
tar -xzf auto-money-spark.tar.gz
cd auto-money-spark
bash scripts/spark.sh setup
```

腳本會檢查 Docker 與 `nvidia-smi`、建置應用程式、啟動具 GPU 存取權的 Ollama、下載模型、先暖機，最後啟動規則比較、獨立 AI 帳戶與儀表板。第一次需要連網下載映像和約 18GB 的模型，所需時間取決於網速。模型權重、執行時上下文與其他程式共同使用記憶體；18GB 不是總記憶體需求。

需要：Spark 上可使用的 Docker Compose（含 `gpus`／`--wait` 支援）、更新的 NVIDIA 驅動與已配置的 NVIDIA Container Toolkit。這份包不會自動修改驅動或安裝系統套件。若 `nvidia-smi` 或 GPU 容器啟動失敗，先依 NVIDIA／Ollama 官方指引修復環境，再重試。映像未固定 x86 平台，讓 Docker 在 Spark 使用 ARM64 映像。

本機 Mac 的資料、API key、Docker volume 與模型檔不在搬機包內。Spark 會開始新的實驗，不會延續 Mac 的帳戶或績效。若 Spark 已有主機版 Ollama，這個版本使用獨立的容器與模型 volume，不會共用已下載模型。

## 打開頁面

在 **Spark 自己的瀏覽器**開啟：

http://localhost:8080

上方依然是兩組規則策略與基準，下方新增「Ollama 本地模型」獨立帳戶區塊。紀錄來源可選：

- **Ollama AI 決策**：建議動作、模型說明、實際成交或風控拒絕。
- **AI 呼叫結果**：每次呼叫成功或失敗；成功回覆不代表成交。
- **AI 行情事件**：市場連線錯誤。

AI 帳戶獨立以 US$1,000 起始、BTC／ETH 共用風控，與兩組規則策略不是同一筆錢。AI 帳戶的起始時間可能不同，所以沒有把它放進原本同期績效曲線。頁面顯示最後決策時間；帳戶存在不代表服務目前存活。

若你想在 Mac 的瀏覽器看 Spark 的頁面，可以使用 SSH tunnel（把下面帳號與主機換成你自己的）：

```bash
ssh -N -L 8081:127.0.0.1:8080 你的帳號@你的Spark主機
```

然後在 Mac 開啟 http://localhost:8081。使用 8081 是為了與 Mac 目前的 8080 儀表板分開。需要你原有的 SSH 存取權；不會在公網或區域網路直接開放模型 API。

## 平常使用

```bash
# 查看所有服務
bash scripts/spark.sh status
# 查看最近的 AI 決策或錯誤
bash scripts/spark.sh logs
# 檢查 Ollama 是否使用 GPU；觀察 PROCESSOR 與模型欄位
bash scripts/spark.sh gpu
# 暫停規則組和 AI 組交易，保留服務
bash scripts/spark.sh pause
# 恢復交易，但不解除已鎖定的日損限制
bash scripts/spark.sh resume
# 停止全部服務，保留資料
bash scripts/spark.sh stop
# 下次重新啟動，不重下載模型
bash scripts/spark.sh start
```

不要執行 `docker compose down -v`；那會刪除模擬帳本或模型 volume。修改模型或風控後，舊 AI 帳戶會拒絕混用新設定；請建立另一個實驗資料 volume。`spark.sh` 預設固定 `qwen3.8:27b`；若要另一個已確認模型，可在建立全新實驗前 `export OLLAMA_MODEL=完整標籤`。此變數以 shell 值為準，腳本不從 `.env` 讀取模型選擇。

## 若一直沒有成交

- **模型載入或網路失敗**：看 `logs` 與頁面「AI 呼叫結果」。模型不會自動下載，首次需執行 `setup`。
- **回覆太慢**：預設推論逾時 120 秒，行情有效期限仍為 60 秒。暖機可減少初次載入等待；若仍慢，先檢查 GPU 使用情形。不能把行情有效期限調大來勉強成交。
- **格式不合規**：拒絕工具呼叫、截斷回覆、額外欄位或不合法數字，改為 HOLD。
- **成本門檻未通過**：目前仍由本機 SMA 差距代理值與來回成本比較，AI 不能自行宣稱預期收益來繞過。這次只加入模型推論，沒有解決此前提出的策略校準或歷史回測問題。

AI 每輪依序分析兩種幣，完成後等待五分鐘。推論會增加週期長度，不是精準每五分鐘觸發。模型沒有工具權限，不能自行改檔案、修改參數、提高本金或啟用 LIVE。提示和回覆保存在 Spark 本機帳本，不傳送給 OpenAI；公開行情仍需要連到 Binance。Ollama 請求關閉 thinking、使用 JSON schema、固定溫度 0、32K 上下文與最多 2048 個輸出 token；這些設定有助於規範輸出，但不保證可重現或獲利。

## 驗證範圍與來源

已以 mock provider 測試請求格式、錯誤轉 HOLD、雙幣帳本、共用停止開關與資料呈現。**未登入你的 Spark，未在該機器執行 GPU 推論，也未實測 Qwen 的交易表現。**

- [Ollama Qwen3.8 27B 模型標籤](https://ollama.com/library/qwen3.8:27b)
- [Ollama native chat API](https://docs.ollama.com/api/chat)
- [Ollama Docker 與 NVIDIA GPU](https://docs.ollama.com/docker)
- [Ollama DGX Spark 支援](https://ollama.com/blog/nvidia-spark)
- [NVIDIA Spark 的 Ollama 工作流程](https://build.nvidia.com/spark/open-webui)
