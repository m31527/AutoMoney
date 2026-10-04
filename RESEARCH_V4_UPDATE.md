# 進場研究 v4：正確成本、退出語意與盲測案例

## 修改內容

- 程式計算成本百分比：40 bps = 0.40%。提供 `目前 last price × (1 + 來回成本 bps / 10000)` 作為近似損益兩平參考價。它不是未來價格預測、精確成交門檻或保證獲利。
- 主要模型輸入移除均線差距代理值；原本預篩紀錄保留，正常模擬策略與風控不變。
- 明確說明：目前可賣金額為零是因為沒有庫存；買入後可以依當時餘額、交易所限制與風控申請退出，但不保證成交。
- 回條新增成本百分比、未來退出語意。回條只是欄位接收檢查，仍需分析回答是否自相矛盾。
- OpenTeddy 研究改為每 30 分鐘一筆，停止兩種提示詞成對呼叫；每組呼叫上限仍約 48 次／24 小時，兩組合計約 96 次。服務共用額度不變。
- 重新建置 dashboard，讓匯出摘要使用新版程式。

## DGX 更新

```bash
cd ~/AutoMoney
git pull --ff-only
python3 scripts/install-openteddy-bridge.py ~/OpenTeddy
cd ~/OpenTeddy
./openteddy service restart
cd ~/AutoMoney
bash scripts/openteddy-shadow.sh btc-eth
```

OpenTeddy 若為 system 服務，重啟加 `--system`；若使用 Docker 或手動啟動，依原啟動方式重建／重啟。只有已在跑 SOL/XRP 實驗時，再執行：

```bash
bash scripts/openteddy-shadow.sh sol-xrp
```

健康檢查應顯示 `version: 3`。新研究紀錄應為 `compact-entry-v4`，不是用健康檢查版本當成研究版本。資料卷、既有模型、token、API key 保留，不會自動啟用商業模型。舊紀錄不刪除，分析時應依 policy_version 分開。

先收集 1–2 小時確認回條與版本，之後持續 8–12 小時。研究仍不下單。最後四小時缺少完整後續價格是正常現象；不把尚未到期案例當失敗。

## 固定案例重播

新增 `python -m trader.replay`。它從 ZIP 摘取全部可重播的研究輸入、移除未來價格和原建議、去除同快照重複案例，不依照事後漲跌挑選案例。不會讀取或呼叫 OpenTeddy 的工具。

在已有 AutoMoney Python 環境中（Docker 映像也包含此模組）：

```bash
python -m trader.replay paper-analysis.zip --output cases.jsonl
```

這一步**不呼叫模型**。輸出若已存在則停止，避免覆蓋既有測試。

只有明確加 `--run` 才呼叫環境變數 `OPENTEDDY_URL`、`OPENTEDDY_TOKEN`、`OPENTEDDY_PROVIDER`、`OPENTEDDY_MODEL` 指定的模型：

```bash
python -m trader.replay cases.jsonl --run --limit 12 --output local-answers.jsonl
```

預設最多 12 次，可設定 1–24 次；遇到第一個錯誤即停，不自動重試或改模型。輸出包含同一 `case_id`、建議、模型、usage 與輸入核對資訊，沒有任何訂單。重播也消耗 bridge 的每日額度；它不會為歷史行情模擬成交。不要把測試答案寫進輸入案例。

本版沒有自動開啟或同時調用商業模型。商業比較需要另外明確啟用伺服器允許的 provider/model；目前 bridge 固定一組模型，切換也會影響持續研究，應安排獨立 bridge 或先暫停研究後再切換。對照時使用相同 cases 檔、相同 limit、相同研究版本，並單獨記錄模型費用。

已用 10/4 11:11–23:11 匯出資料驗證案例整理：24 次成對回覆整理成 12 個唯一案例，全部保留、不依結果篩選。本機未實際呼叫本地或商業模型。
