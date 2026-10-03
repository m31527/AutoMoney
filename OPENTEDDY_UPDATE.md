# OpenTeddy 影子研究部署（DGX Spark / Linux）

這一版將「被預篩擋下的候選行情」交給 OpenTeddy 的專用分析介面，沿用每小時一組、同份行情兩種提示詞的研究。回答只記錄，不送進交易引擎。原本 Ollama 模擬帳戶、規則策略、預篩與風控不變。

支援指定 Ollama 或 OpenAI；不經過 OpenTeddy 的 planner、工具、記憶或自動升級模型。預設是本地 Ollama。OpenTeddy 仍使用原本的服務，不增加容器。

## 1. 放入更新檔

將 `openteddy-shadow-update.tar.gz` 放到 DGX 的 `~/AutoMoney`，執行：

```bash
cd ~/AutoMoney
tar -xzf openteddy-shadow-update.tar.gz
python3 scripts/install-openteddy-bridge.py ~/OpenTeddy
```

安裝器會將 3 個 `automoney_*.py` 模組放入 OpenTeddy，在 `main.py` 加入路由，第一次修改前備份為 `main.py.before-automoney`。它會建立兩邊共用的隨機金鑰，檔案權限為 600；重跑不覆蓋既有設定。不修改 OpenTeddy 原本的 API key。請勿分享 `.automoney.env` 或 `config/openteddy.env`。

## 2. 重啟原本的 OpenTeddy

如果是 CLI 安裝的使用者服務：

```bash
cd ~/OpenTeddy
./openteddy service restart
```

若原本是 system 服務，使用 `./openteddy service restart --system`；若是手動執行 `run.sh`，重新啟動原本那個程序即可。不要同時再開第二套。Docker 版 OpenTeddy 須重建其映像，僅在主機修改檔案不會更新容器。

## 3. 檢查 URL，再啟動兩組

```bash
cd ~/AutoMoney
nano config/openteddy.env
```

預設：

```dotenv
OPENTEDDY_URL=http://127.0.0.1:8000
OPENTEDDY_PROVIDER=ollama
OPENTEDDY_MODEL=qwen3.8:27b
# OPENTEDDY_TOKEN 保留安裝器產生的值
```

本部署對 ai-trader 使用 Linux host network，因此這裡的 `127.0.0.1` 指 DGX 主機。若 OpenTeddy 是主機 8001 port，改成 `http://127.0.0.1:8001`。不需要將 OpenTeddy 整個服務開放到區網。現有 `config/ollama.env` 的 Ollama URL 必須仍能從主機網路存取。

```bash
bash scripts/openteddy-shadow.sh btc-eth
bash scripts/openteddy-shadow.sh sol-xrp
```

腳本先建置，再從 Docker 內呼叫有驗證的 `/automoney/health`；失敗會停止，不替換執行中的服務。健康檢查不會呼叫模型、不產生 token 費用。成功會顯示 `status: ok`，才啟動更新後的 ai-trader/dashboard。若沒有 SOL/XRP 實驗，只執行第一行。

已有影子研究紀錄時，會沿用原本每小時冷卻時間，剛重啟不一定立刻出現新紀錄。兩組合計最多約 96 次研究呼叫／24 小時，額度由 OpenTeddy 共用，失敗也計次。已有的正常 Ollama 策略呼叫不包含在這個額度內。

## 4. 若要改用商業模型（會付費）

先完成本地模式連線驗證。修改 `~/OpenTeddy/.automoney.env`：

```dotenv
AUTOMONEY_PROVIDER=openai
AUTOMONEY_MODEL=gpt-5.4
AUTOMONEY_ALLOW_OPENAI=true
AUTOMONEY_DAILY_CALL_LIMIT=24
```

保留原本 `AUTOMONEY_TOKEN`。模型名稱必須是你的帳戶可使用的名稱；此更新未實際驗證任何商業模型權限。OpenTeddy 已設定的 OpenAI key 會由其既有 config 載入，不需要放進 AutoMoney。

同時修改 `~/AutoMoney/config/openteddy.env`：

```dotenv
OPENTEDDY_PROVIDER=openai
OPENTEDDY_MODEL=gpt-5.4
```

重啟 OpenTeddy，再執行第 3 節兩行啟動指令。24 次為兩組共用的滾動 24 小時額度，約 6 小時即可用完兩組每小時兩次的配額；後續呼叫會記錄 429，不會改用其他模型。每次輸出上限 2048 token，120 秒逾時，沒有自動重試。這是次數／token 限制，**不是美元費用硬上限**，請另在供應商設定預算。若兩組都要連續跑 24 小時，須自行將每日次數調高，最高 96。

## 5. 匯出與判讀

繼續使用原本頁面的日期區間匯出 ZIP，建議更新後累積 8–12 小時，再留 4 小時讓最後一批的後續行情完整。

匯出 `shadow_ai.results` 新增 `provider` 與 `provider_metadata`：實際回報模型、request_id、token 用量、推論耗時／錯誤碼。`shadow_ai.by_provider_model` 分組統計，舊紀錄保留。頁面原本「Ollama 本地模型」仍表示正常模擬帳戶，不能把它當成 OpenTeddy 研究呼叫次數。

本版比較同一模型的兩種提示詞；跨日期更换商業模型後的結果，受到行情不同影響，不能單憑報酬差異宣稱模型更好。影子 BUY 不建立持倉，也不是實現獲利。

## 回復舊模式

```bash
cd ~/AutoMoney
bash scripts/ai-shadow-pair.sh btc-eth
bash scripts/ai-shadow-pair.sh sol-xrp
```

原本資料卷保留。OpenTeddy 新路由可留著不用；要移除時刪掉 `main.py` 尾端 `# AutoMoney isolated research router` 與後面兩行，再重啟。不要直接用舊備份覆蓋後來更新過的 main.py。

## 已驗證與限制

本機已測試介面驗證、限額、錯誤輸出、無重試及影子建議不成交；以模擬上游回應測試，不會呼叫付費模型。DGX 的實際連線、OpenTeddy 遠端版本及模型可用性，仍由部署健康檢查與第一筆研究驗證。請以單一 OpenTeddy worker 執行；並行限制為程序內鎖，次數限額則使用持久 SQLite、重啟不歸零。
