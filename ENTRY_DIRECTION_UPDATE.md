# 進場方向實驗（2026-09-24）

此版的目的在於研究進場品質：原本 SMA 差距採絕對值，下跌時也可能超過成本門檻；現在新增明確的買入方向條件。原有空倉成本預篩保留，信心、費用、滑價、冷卻與部位上限均不降低。

## 進場規則

使用最近 20 根已收盤且連續的 1 小時 K 線：

- SMA(5) > SMA(20)：UP，允許繼續評估買入，但不是自動買入。
- SMA(5) < SMA(20)：DOWN，不買入。
- 相等：FLAT，不買入。
- 缺少資料、非有限／非正收盤價、時間缺口或資料過期：UNAVAILABLE，不買入。

沿用每小時趨勢策略的資料時效邊界：最後已收盤 K 線的開盤時間距評估時間不超過 2 小時。尚未收盤與未來 K 線不計入指標。

空倉時先經既有成本預篩，再檢查方向，兩者通過才詢問模型。帳戶有任何持幣（含零碎餘額）仍保留模型評估以處理出場；但若模型提出 BUY，程式仍強制檢查 UP，不能藉由已有持倉繞過方向限制。SELL 不受新增買入方向條件限制，仍受既有出場風控約束。

模型會收到已計算的方向、兩條均線與帶正負號的差距。提示明確區分絕對值代理訊號與向上趨勢，不要求模型把信心提高到門檻。空倉預篩版本為 flat-cost-direction-prefilter-v2，模型提示／建議審查版本為 ai-entry-direction-v3。

這是待驗證的實驗，不是已證明較好的策略。它可能避免部分逆勢進場，也可能錯過下跌後的反彈；是否值得保留，要看後續數據。

## Spark 更新與啟用

把 entry-direction-update.tar.gz 放到 ~/AutoMoney：

```bash
cd ~/AutoMoney
tar -xzf entry-direction-update.tar.gz
bash scripts/ai-prefilter.sh direction btc-eth
```

若 SOL/XRP AI 組也要測試：

```bash
ALT_DASHBOARD_BIND=0.0.0.0 bash scripts/ai-prefilter.sh direction sol-xrp
```

沿用既有 automoney／automoney-altcoins 專案，更新 AI worker 和網頁，不新增模型或帳戶、不重啟 Ollama。設定檔、原本 compose.yaml／compose.ai.yaml、帳本與本金都不在更新包內。8083／8084 的規則組不需要更新，保留作參考。

瀏覽器重新整理一次，8080／8082 應顯示「進場方向實驗已啟用」。更新時刻會記在 AI_PREFILTER_MODE 中。以後啟動這個實驗也用 direction 指令。

回到上一版「只看成本」預篩：

```bash
bash scripts/ai-prefilter.sh start btc-eth
ALT_DASHBOARD_BIND=0.0.0.0 bash scripts/ai-prefilter.sh start sol-xrp
```

start 會明確關閉方向檢查，但繼續記錄方向研究資料。disable 則關閉整個預篩與方向實驗；帳本均保留。沒有使用的組別不用執行。

## 下一輪如何比較進場品質

每次預篩都保存同一份行情在以下兩種政策的判斷，不額外呼叫一次模型：

1. 只看成本，是否允許詢問模型。
2. 成本＋方向，是否允許詢問模型。

summary.json → activity → ollama → direction_study 新增：

- cost_only_allows_model、cost_and_direction_allows_model、extra_direction_skips。
- UP／DOWN／FLAT／UNAVAILABLE 的樣本數。
- 各方向在通過成本門檻後，1／4 小時的後續價格平均與中位數變化，以及有／無可配對資料筆數。

後續價格使用本次匯出內第一筆在目標時間或之後、且不晚於目標時間 10 分鐘的同幣報價。缺少未來資料就留空，不使用較早價格代替。建議匯出更新後至少 24 小時的資料；最後 4 小時內的樣本可能尚未完整配對。

這些是市場價格變化，未扣交易費、買賣價差與滑價，不能當作策略損益。樣本彼此重疊，不是獨立交易；同輸入政策比較也不是兩個平行交易帳戶。最終仍要看實際 PAPER 交易含成本的表現。舊匯出沒有方向資料，不能補造過去趨勢。

AI 呼叫仍有原有預算、行情重查、信心及完整風控。summary 的 ai_diagnostics.entry_direction_blocked 另外統計模型提出不合方向的 BUY 次數；被預篩擋下的循環不會冒充模型回覆。

下次提供更新後相同區間的 8080 與 8082（若有測試）ZIP，重點看方向額外擋下的樣本、其後續價格走勢，以及實際成交／費用／回落。不能只因呼叫更少，就判定進場規則更好。
