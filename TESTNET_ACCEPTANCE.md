# 交易所執行驗收：獨立 Spot Testnet

## 範圍

此命令只會連到 `https://testnet.binance.vision`，使用測試網虛擬資產，與 PAPER 資料卷、AI 策略及正式交易隔離。預設不下單，須明確指定 `--execute-testnet`。沒有正式站 URL 設定選項，不支援出金、槓桿、期貨或自動連續交易。

每次驗收以當時 ask 計算約 50 虛擬 USDT 的 BTC 買入數量，經交易所數量／金額限制取整。市價成交金額仍可能因價格變動而不同。買入確認 FILLED 且成交明細完整後，賣出本次取得、扣除 BTC 手續費後的可賣數量；取整可能留下微量餘額。若不符最小金額則停止，不追加買入。

適用獨立測試帳戶；同時其他交易或测试網重置會造成餘額核對失敗。建議至少有 55 虛擬 USDT。測試網成交情況不能代表正式市場滑價或策略收益。

## DGX 操作

```bash
cd ~/AutoMoney
git pull --ff-only
cp -n config/testnet.env.example config/testnet.env
chmod 600 config/testnet.env
nano config/testnet.env
```

到 [Binance Spot Testnet](https://testnet.binance.vision/) 建立 HMAC API key/secret，填入這兩行。只填測試網金鑰，不要使用正式交易所金鑰，也不要把檔案貼進聊天。該檔已加入 gitignore。

先查看狀態（不呼叫模型、不送出訂單）：

```bash
bash scripts/testnet-acceptance.sh
```

顯示 `NOT_RUN` 是尚未執行，不是驗收失敗。完成金鑰設定後，執行一次虛擬資產往返：

```bash
bash scripts/testnet-acceptance.sh --execute-testnet
```

這個命令不會更新或停止原本的模擬容器。它會保存資料到 `data/testnet-acceptance/`。兩次市價單不是策略交易，只是驗證執行鏈路。

## 報告與重啟

把 `data/testnet-acceptance/report.json` 提供分析，檔案不含金鑰。包含訂單、成交、費用、前後餘額與差異。

- `PASS`：這次 Testnet 往返及餘額核對通過，不代表獲利或正式交易已可上線。
- `FAIL_BALANCE_RECONCILIATION`：前後帳戶差異無法用成交／手續費解釋，需檢查帳戶是否有其他操作或重置。
- `BLOCKED`：查單、驗證或交易所回應未完成；保留資料庫及 run-id，先提供報告，不要刪庫重跑。

訂單 ID 由固定 run-id 加 BUY/SELL 產生。送出前持久化；遇逾時先查原訂單，不盲目重送。重跑相同命令會沿用原 run-id，已 PASS 則只返回原報告。存在未完成 run 時，不允許換一個新 run-id 開始。若資料庫遺失或測試網重置，不能假設重啟機制仍可對應原訂單。

緊急停止此驗收（不影響 PAPER）：

```bash
bash scripts/testnet-acceptance.sh --stop
```

停止會持久化，程式不自動恢復。已有送出訂單仍可能成交；停止不是交易所撤單，也不會自動平倉。應先核查測試網帳戶及保存的訂單紀錄。

## 本輪驗收清單

| 項目 | 本機 | 真正 Testnet |
|---|---|---|
| 正式站／非白名單路徑阻擋 | 自動測試通過 | 不會向正式站送測試請求 |
| 逾時後查单、不重複 BUY | 模擬傳輸故障測試通過 | 尚未故障注入 |
| 持久 journal／重啟去重 | 既有適配器測試通過 | 等 DGX 執行 |
| 部分成交／明細不完整停止 | 自動測試通過 | 尚未故障注入 |
| 持久 kill switch 阻止後續下單 | 既有適配器測試通過 | 等 DGX 執行 |
| 實際下單、成交、費用與餘額 | 測試替身通過 | 等 Testnet 金鑰與 DGX 執行 |
| 獲利優勢 | 尚未證實 | Testnet 本身不能證明 |

本機測試與真實網路驗收分開。報告的 `fault_injection` 保持 `NOT_RUN_ON_REMOTE_EXCHANGE`，不會把模擬故障測試當成遠端故障驗收完成。

官方依據：[Testnet API](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md)、[逾時執行狀態未知與查單要求](https://developers.binance.com/en/docs/products/spot/rest-api)。

## 模型比較是另一項待驗證工作

現有 `artifacts/model-comparison` 的準備檔不等於商業模型回答；本機尚未取得使用者 DGX 的 `answers.jsonl`／`comparison.json`。使用 `MODEL_COMPARE_UPDATE.md` 的獨立六案例比較流程，提供該結果目錄。不要以 Testnet 成功替代模型策略驗證，也不要為了取得資料反覆重跑已計費的比較。
