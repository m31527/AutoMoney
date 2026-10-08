# 48 小時策略驗收

此服務使用現有 HourlyTrendStrategy（1h SMA 5/20）提出訊號，以正式市場公開行情作為輸入，在 Binance Spot Testnet 成交。不是 AI 實盤，也不能用 Testnet 報酬推定實盤報酬。原 PAPER、OpenTeddy 研究組不變。

獨立專案 automoney-soak、資料 data/testnet-soak；僅掛載 config/testnet.env。不使用整個測試帳戶的大量贈送資產：內部預算 100 USDT，僅能賣出此服務買入的 BTC。單筆 15 USDT、進場總額 30 USDT、每日最多 4 筆、交易間隔至少 30 分鐘。每日虧損 2 USDT 或累計虧損 5 USDT 停止買入。這些為下單前限制，市價成交波動可能使實際結果超出估值，並非保證損失上限。

進場沿用保守成本門檻（40 bps 費用與滑價 + 買賣價差 + 5 bps）；超過 20 bps 價差、過期行情、Testnet 與公開市場偏離超過 1% 不下單。程序不會為了湊交易而降低門檻。沒有交易時，此次觀察不能算完成策略成交驗收。

啟動：`bash scripts/testnet-soak.sh start`
狀態：`bash scripts/testnet-soak.sh status`
紀錄：`bash scripts/testnet-soak.sh logs`
持久停機：`bash scripts/testnet-soak.sh stop`

啟動後 48 小時停止新增決策，不自動平倉；任何餘倉必須列入報告。重啟沿用原期限與帳本。異常會持久停機、不自動重試，不提供自動解除停機入口。不要刪除資料庫。執行期间請不要在同一 Testnet 帳戶操作 BTC/USDT，否則餘額漂移會停止服務。

## 通過条件

* 完整 48 小時觀察，至少 500 輪成功紀錄，最大評估間隔不超過 15 分鐘。
* 至少一筆由策略觸發且核對成功的買入與賣出；沒有訊號不能強迫交易。
* 無重複送單、無未解訂單、無餘額差異；手續費與殘餘持倉完整記錄。
* 所有 BLOCKED 必須處理並留下原因；不能靠刪除資料重測算通過。

## 正式交易放行：目前 BLOCKED

現有 BinanceSpotAdapter 仍拒絕 LIVE，TestnetTransport 仍拒絕 production 網址。即使設定 ENABLE_LIVE_TRADING=true 也不能令此服務下真單。

上線前還需：上述觀察通過、正式 API 權限唯讀檢查（禁提領、IP 限制、僅現貨）、使用者確認實際本金/單筆/總額/虧損上限、獨立正式環境與金鑰、綁定版本與短效人工授權的正式下單入口。這些尚未完成，不能把隔離測試通過稱為實盤可用。

目前沒有正式寫入 transport，因此「無法誤下真單」可驗證，「正式交易可成功」尚不可驗證。不要沿用 Testnet 金鑰，也不要將正式金鑰寫入 example 檔案或 Git。

新限額版本 initial-100-v1；僅在尚未成交、無待處理訂單、無持倉時自動切換；保留事件，重新起算 48 小時。不自動增加到 300 或 1000 USDT。

唯讀放行報告：`bash scripts/testnet-soak.sh readiness`。尚未完成的正式入口與權限檢查會明確列為 BLOCKED，不以等待時間取代人工檢討。
