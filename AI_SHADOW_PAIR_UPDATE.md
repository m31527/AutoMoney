# 影子提示對照 v2

每小時最多一組兩次串行呼叫，帳戶全幣種共用持久化冷卻。每小時呼叫上限與舊版每30分鐘一次相同，但單輪等待時間可能加倍，實際耗時／GPU負擔依輸出變動。僅對原預篩已擋下的行情抽樣，不能代表所有行情。

- control：沿用shadow-entry-v1提示与輸入。
- unanchored：移除輸入中deterministic_edge_proxy_bps及提示對它的引用，並說明預算只是上限，不能僅因100美元猜測不符合最低訂單限制。原行情、閉合K線、預算、評估時間相同。
- 新版不要求買入，不改信心／成本／部位風控；任何影子建議都不送單。
- 同組共享pair_id與snapshot_context_sha256，每組交替先後順序；第二次使用原快照，不接收第一次回答。
- summary.json → activity.ollama.shadow_ai.prompt_comparison：完整成功配對數、動作不同的配對數、各版动作統計。results包含各版信心、原因、耗時与逐筆未來配對。
- 兩版都完成後才選共同報價起點，計算1/4h價格變化減預估成本。不是实际成交收益；SELL不能當作放空。錯誤／跨匯出邊界不完整配對不算有效對照。整體actions仍包含舊版及不完整樣本，分析以pair_id為準。
- 這次同時改了代理指標暴露與預算說明，能驗證這組改法，不能單獨歸因其中一項。樣本相關、非隨機市場抽樣；更多BUY不等於更好。

## Spark

將ai-shadow-pair-update.tar.gz放入~/AutoMoney：

```bash
cd ~/AutoMoney
tar -xzf ai-shadow-pair-update.tar.gz
bash scripts/ai-shadow-pair.sh btc-eth
```

沿用既有帳戶及容器，更新AI worker和8080匯出，不新增服務；8083不必更新。不重設資金或資料庫。

8–12小時後匯出8080和8083相同範圍。預期最多約8–12組、16–24次額外呼叫，正常交易路徑的模型呼叫不包含在此上限。行情、冷卻及推論可能減少實際組數。4h尾端樣本可能未成熟，可在下次匯出包含前次末4小時補齊；分析時以pair_id去重。

回到上一版單提示：bash scripts/ai-shadow.sh btc-eth。關閉影子：AI_SHADOW_INTERVAL_SECONDS=0 bash scripts/ai-shadow.sh btc-eth。

此包未啟用Testnet或真實交易。工程測試不代表策略已證明獲利。
