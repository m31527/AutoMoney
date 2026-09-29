# AI 影子評估 v1

目的：即使成本／方向預篩擋下交易，仍定期詢問模型，開始收集可評估建議。只針對被擋候選，不代表所有行情的無偏抽樣。原PAPER成交與風控維持；額外建議從不傳入執行器。

每帳戶全幣種合計最多30分鐘1次（可由AI_SHADOW_INTERVAL_SECONDS調整，最短900秒；0關閉）。優先只在BTC/ETH啟用，以控制GPU負擔。由目前輪到且被擋的幣觸發，不保證每幣等量。仍在原worker串行執行，最多增加provider既有timeout的等待，可能拉長觀測間隔。暫停開關啟用不發新呼叫；已進行的呼叫可能完成，但不下單。重啟／失败亦遵守SQLite持久化冷卻。

結果在system_events的AI_SHADOW_STARTED／AI_SHADOW_COMPLETED；summary.json → activity → ollama → shadow_ai。原ai_calls及畫面模型次數不含影子呼叫，不能再以原呼叫0判斷完全沒有推論。匯出保存建議、信心、原預篩、SMA基準動作、耗時、模型與提示雜湊、錯誤；不保存URL或完整提示。建議未經正式交易風控，executable永遠false。

post_response_forward從回答後10分鐘內首筆同幣報價作起點，再找1/4h後10分鐘內首筆。缺報價不配對；提供BUY價格變化減當時成本、現金0作對照，不是成交損益，不可把SELL解讀成放空。多筆結果重疊相關。指令要求4h研究，但模型實際horizon仍原樣保存；不能把任意建議當成完全相同策略。

## Spark

將ai-shadow-update.tar.gz放到~/AutoMoney：

```bash
cd ~/AutoMoney
tar -xzf ai-shadow-update.tar.gz
bash scripts/ai-shadow.sh btc-eth
```

不新增容器、不重啟Ollama、不重設本金。8–12小時預期至多約16–24筆影子呼叫（啟動時點、行情、暫停及推論耗時會減少），4h樣本尾端不完整。下一次匯出相同期間8080和8083；只8080有影子資料。可透過`docker logs --tail 30 automoney-ai-trader-1`檢查worker是否正常。

若要關閉影子並保留原方向策略，執行`AI_SHADOW_INTERVAL_SECONDS=0 bash scripts/ai-shadow.sh btc-eth`。日後用其他啟動腳本可能移除影子環境設定，啟用時統一使用ai-shadow.sh。

## 交易所測試環境狀態

現有Binance介面仍使用ReadOnlyHTTPTransport；這個更新不啟用任何真實／Testnet送單。Testnet下單、成交、餘額對帳、斷線恢復与重複下單測試尚待建置與驗證，不能宣稱已完成。需要獨立測試網帳戶與API金鑰（只放機器設定，不貼聊天），與PAPER及正式帳戶隔離。
