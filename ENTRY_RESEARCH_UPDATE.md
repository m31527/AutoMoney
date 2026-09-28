# 進場研究更新

只擴充離線匯出分析，交易規則、成本預篩、部位上限與模型呼叫頻率不變。

ZIP 新增 ENTRY_RESEARCH.md（可直接閱讀），各帳本 entry_research.json（逐筆配對證據）；summary.json activity 下新增 entry_research。包括所有空倉且行情新鮮的候選，即使成本未通過；按幣、方向、成本是否通過與固定5m/1h強度分箱，計算後續1/4h價格變化、扣除當時預估來回成本的差額、有效及缺失配對數。原 direction_study 保留舊定義。

從事件時間往後1/4h，取同幣第一筆目標之後且不超過10分鐘的報價；只用本次匯出內資料。舊資料無方向欄位無法補造；新版方向事件可直接重新匯出分析，不必重新累積。樣本重疊，不是獨立交易；差額不是成交收益，不做自動選參或改策略。

## Spark 安裝

把 entry-research-update.tar.gz 放入 ~/AutoMoney 後：

```bash
cd ~/AutoMoney
tar -xzf entry-research-update.tar.gz
docker compose -p automoney --env-file config/ollama.env -f compose.yaml -f compose.ai.yaml up --build -d --no-deps dashboard
```

僅重建8080儀表板，AI worker不需重啟，帳本保留。若既有8082 SOL/XRP組也要更新：

```bash
ALT_DASHBOARD_BIND=0.0.0.0 docker compose -p automoney-altcoins --env-file config/ollama.env -f compose.yaml -f compose.ai.yaml -f compose.altcoins.yaml up --build -d --no-deps dashboard
```

重新匯出已含方向紀錄的區间，檢查ZIP有ENTRY_RESEARCH.md即可。最後4小時的4h樣本可能未配對，缺失會明確標示。選較完整的區間以減少尾端缺失，不必另外啟動服務或模型。
