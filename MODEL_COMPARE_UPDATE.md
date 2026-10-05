# 固定 v4 的同案例模型比較

v4 提示詞、成本與退出規則保持不變。新增獨立 `/automoney/replay` 入口，允許明確指定本地或 OpenAI 模型；模型由伺服器白名單限制。它不修改持續收集使用的 `/analyze` 模型設定。

預設只整理案例，不呼叫模型。實際比較須在伺服器啟用付費重播，並執行帶 `--run-cloud` 的命令。每次預設六個唯一案例、最多十二次呼叫（本地六次、商業六次），每天重播額度預設十二次，與持續收集額度分開。額度持久化且失敗也計次；不是美元費用硬上限。

## 更新 DGX

```bash
cd ~/AutoMoney
git pull --ff-only
python3 scripts/install-openteddy-bridge.py ~/OpenTeddy
```

原本 v4 持續收集設定保留，不需要停掉 trader 或重新切換它的模型。

## 準備比較設定（此步不會呼叫模型）

以下採用你先前 OpenTeddy 顯示已設定的 OpenAI 模型名稱。若你的帳戶使用其他名稱，替換 `gpt-5.4`。本機未驗證你的帳戶模型權限。

```bash
python3 scripts/configure-model-replay.py ~/OpenTeddy --cloud-model gpt-5.4
cd ~/OpenTeddy
./openteddy service restart
cd ~/AutoMoney
```

若是 system 服務，重啟加 `--system`；Docker 或手動程序依原方式重啟／重建。重播沿用 OpenTeddy 的 OpenAI key，不需將 key 複製到 AutoMoney。要關閉付費重播，將 `~/OpenTeddy/.automoney.env` 的 `AUTOMONEY_REPLAY_ENABLED` 改為 `false` 並重啟。

## 先準備，不付費

將匯出 ZIP 放到 `~/AutoMoney`，例如：

```bash
bash scripts/model-compare.sh paper-analysis-20261004-233358.zip
```

這會建置專用命令映像，產生 `artifacts/model-comparison/run-時間/` 的 `cases.jsonl`、`plan.json`，不呼叫模型。Linux/DGX 使用 host network 連接主機 OpenTeddy。沿用 `config/openteddy.env` 的 URL/token。

## 執行一次小量比較（會有商業 API 費用）

```bash
bash scripts/model-compare.sh paper-analysis-20261004-233358.zip --run-cloud gpt-5.4
```

使用全部可重播快照去重後，按時間取最早六個案例，並非挑出後來上漲的案例。v3 快照也會用固定 v4 正規化後重播，因此新的模型間比較使用完全一致的 v4 輸入，不把歷史 v3 答案當成 v4 答案。

本地模型讀取 `config/openteddy.env` 的 `OPENTEDDY_MODEL`，預設 qwen3.8:27b；雲端模型必須與設定命令一致。不會自動換模型或重試。兩個模型交替先後順序，任何一筆錯誤就停止，保留部分結果；不要立即重跑以免重复付費。

重播與持續研究使用相同硬體、共用推論鎖。設定和額度分開不等於資源完全隔離；若出現 BUSY/429，本輪會停止，需檢查錯誤碼再安排下一次執行。

## 結果給我哪些檔案

完成後將終端顯示的整個結果目錄與原本 ZIP 一起提供。結果目錄包含：

- `cases.jsonl`：實際固定模型輸入，不含後續價格／原建議。
- `plan.json`：案例清單、選擇方式、指定模型與最多呼叫次數。
- `answers.jsonl`：兩模型回答、實際模型回報、token、耗時與錯誤。
- `comparison.json`：完成／未完成配對、BUY/HOLD 分布、動作分歧及用量。

報告只把兩模型都成功且輸入雜湊相同的案例納入配對。錯誤不算 HOLD。模型信心不當作準確率；BUY 較多也不代表較好。`cost_usd` 留空，token 不能直接當美元，失敗請求也可能有未回報費用。後續行情評估需與原本 ZIP 對照，未來價格不會送給模型。

這次新增的是執行與比較流程，不是已完成的商業模型成效驗證。本機只做模擬上游測試與免費案例整理。
