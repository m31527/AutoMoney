# 空倉成本預篩實驗（2026-09-24）

此版讓既有 AI worker 在整個帳戶完全沒有持幣時，先用現行的成本與 SMA 訊號代理值，判斷買入是否已被成本規則排除。若已排除，就記錄一次本機觀望並跳過模型；不降低信心、費用、滑價或其他風控限制。

## Spark 更新

把 `ai-prefilter-update.tar.gz` 放到 `~/AutoMoney`，執行：

```bash
cd ~/AutoMoney
tar -xzf ai-prefilter-update.tar.gz
bash scripts/ai-prefilter.sh start btc-eth
```

你另外有 SOL/XRP AI 組，若也要套用：

```bash
ALT_DASHBOARD_BIND=0.0.0.0 bash scripts/ai-prefilter.sh start sol-xrp
```

腳本使用既有專案名 `automoney`／`automoney-altcoins`，只重建它們的 `ai-trader` 與 `dashboard`。不新增模型服務，不重啟 Ollama；8083／8084 的純規則實驗不需要更動。若原專案名不是這兩個，先依現有 Compose 專案名調整腳本，避免新建另一份帳戶。8080 沿用現有 compose.yaml，8082 的區網綁定沿用上述環境變數；若只供本機使用，省略該變數。

更新包不含原本 compose.yaml、compose.ai.yaml、設定檔或資料庫；原模型 URL、模型、帳本與本金不變。額外的 compose.ai-prefilter.yaml 只設定 AI_PREFILTER_ENABLED。它預設在新腳本中開啟；未使用覆寫檔時維持舊行為。

更新完成後瀏覽器重新整理一次，AI 區塊應顯示「空倉預篩已啟用」。之後仍自動更新。首次啟動就會記錄模式，不需要等一次模型呼叫。舊的「最近推論」時間仍指最近一次真正呼叫的耗時，跳過推論時不更新它。

之後請用新腳本啟動這兩組 AI；若改用舊 ai.sh／altcoins.sh 重建，可能移除預篩覆寫而回到舊模式。可用網頁的狀態確認。

## 關閉實驗，恢復原本每輪呼叫

```bash
bash scripts/ai-prefilter.sh disable btc-eth
ALT_DASHBOARD_BIND=0.0.0.0 bash scripts/ai-prefilter.sh disable sol-xrp
```

這是重新設定既有 worker，不刪資料。若只啟用 BTC/ETH，就只需關閉 BTC/ETH。

## 跳過條件與限制

必須同時滿足：

1. 整個帳戶所有持幣數量都是零；任何幣有持倉，即使是零碎餘額，都保留模型評估。
2. 目標報價在原本允許的新鮮度範圍內。
3. 根據同一份行情，預估來回成本超過最大成本，或 SMA 差距代理值減成本不足原本淨門檻。

這不是模型拒答，也不是模型失敗；執行紀錄顯示「空倉且買入成本門檻未通過，本輪省略模型推論」。成本檢查通過只代表可以詢問模型，不代表允許成交；原有預算、推論後報價檢查與完整風控照常執行。

此實驗使用當下資料作篩選，會改變決策取樣，不保證與每輪詢問模型的結果完全相同。它依賴既有 SMA 代理門檻，不能視為驗證了該門檻能預測獲利。兩幣評估完成後仍休息 300 秒；跳過推論會縮短整輪時間，因此不能直接拿歷史日呼叫數相減，宣稱省下相同百分比的電力。

若帳戶有零碎部位，預篩不會跳過；此保守設計避免漏掉出場處理。單次真正推論時 GPU 使用率仍可能很高，主要預期改善的是整段時間的推論次數與忙碌時間。

## 匯出分析

仍用原來的 ZIP 匯出與時間區間。新增 summary.json → activity → ollama → ai_prefilter：

- evaluations：啟用預篩時的評估次數。
- skipped_model_calls／skip_pct：省略次數與該評估樣本內比例。
- reasons／policy_versions：判斷原因與 flat-cost-prefilter-v1 版本統計。

每次評估的價格成本代理值、是否完全空倉、跳過原因及 cycle_id 都記在 ollama/events.jsonl，與實際決策相連。AI_PREFILTER_MODE 記錄啟動時開關狀態；AI_PREFILTER_EVALUATED 記錄逐次判斷。被跳過的循環不建立虛假的 ai_calls 或推論耗時。

更新前後保留在同一帳戶，因此請選更新後區間分析。網頁的省略次數是累計值，匯出的統計是所選區間值；不得把跳過量當成交、獲利或實測 GPU 耗電。模型呼叫依請求開始時間、預篩依評估時間、決策依完成時間篩選，邊界筆數可能不同。

建議下一次提供 8080 的更新後 ZIP；SOL/XRP 也啟用時，再提供 8082 的 ZIP。8083 仍可供規則出場比較，但不會包含 AI 預篩統計。
