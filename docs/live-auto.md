# 自動實盤（預設關閉）

這是使用者自行啟用的真實 BTCUSDT 現貨交易程式，不是模擬。每 5 分鐘評估
已收盤的 1 小時 SMA(5/20) 趨勢，保留 45 bps 加價差成本門檻。
目前使用規則策略，不使用 Ollama，也不代表策略已有獲利優勢。

## 限額與啟用條件

本金 80 USDT、單筆最多 15、總持倉最多 30；每日 2／累計 5 USDT 虧損停買，
每天最多 4 筆、間隔至少 30 分鐘，沿用既有正式帳本。虧損門檻不是保證止損，
程式沒有交易所端止損單；遇到故障停止送單時仍可能持有 BTC。

仍須 Testnet 驗收通過、正式 API 權限及餘額核對成功、IP 清單人工核准、
沒有待確認訂單或持久停機。安裝本功能不會繞過這些條件。

## 在 DGX 自行啟用

1. 更新程式後執行 `bash scripts/live-auto.sh build`。
2. 在 `config/live.env` 明確設定 `ENABLE_LIVE_TRADING=true` 及
   `ENABLE_AUTO_LIVE=true`，保留正確金鑰和 `LIVE_APPROVED_EGRESS_IPS`。
3. 執行 `bash scripts/live-control.sh start` 載入控制頁設定。
4. 在加密連線的控制頁檢查帳戶，確認沒有未完成訂單後「核對後解除停機」。
5. 執行 `bash scripts/live-auto.sh arm`，本人輸入畫面要求的確認文字。
   不符合驗收、停機或帳戶條件會拒絕，不應修改資料庫繞過。
6. 執行 `bash scripts/live-auto.sh start`，這一步會開始自動真實買賣。

檢查：`bash scripts/live-auto.sh status`、`bash scripts/live-auto.sh logs`。
停止：`bash scripts/live-auto.sh stop`，先設持久停機，再停止 worker 並取消授權。
停止不會自動平倉，也無法撤回已送達交易所的市價單。

## 故障與紀錄

單一帳本鎖將自動與人工操作串行化；自動模式已授權時不接受人工準備或放行。
每個五分鐘時段先持久保存已處理標記；重啟不重播同時段決策。
先保存送單意圖再送一次 POST，完整成交及費用與帳戶核對成功後才入帳。
有待確認訂單時只查原單並保持停機，絕不重送；對帳後須重新人工解除及授權。
權限、IP、帳戶、網路或未知異常會停機並取消自動授權，不自動恢復。
一般 HOLD、成本門檻、交易次數／冷卻限制會記錄後等待下一輪。

事件保存於 data/live/live.db 的 live_events，包括 AUTO_ARMED、AUTO_DECISION、
AUTO_WAIT、AUTO_REJECTED、AUTO_APPROVED、FILL、AUTO_BLOCKED 等。
控制頁顯示自動授權狀態及最近結果；「已授權」不表示 worker 一定存活，
還需查看最近輪次時間及 Docker 狀態。沒有郵件或外部通知。

自動 worker 使用獨立 compose profile，restart=no。一般控制頁部署不會啟動它。
此次交付僅新增程式及測試，未將 DGX 的實盤或自動開關打開。
