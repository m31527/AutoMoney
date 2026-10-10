# 正式環境：逐筆人工放行

此版本補齊正式 API 權限檢查、正式現貨下單入口與人工放行。沒有自動實盤交易迴圈，也不會因測試時間到了自動開啟真實交易。原 PAPER / Ollama 服務繼續模擬。

## 使用方式

1. DGX `~/AutoMoney` 執行 `bash scripts/live-control.sh setup`，建立不進 Git、權限 600 的 `config/live.env`。已有檔案不覆蓋。
2. 在 DGX 編輯 `config/live.env`，填入正式 HMAC API key / secret，不能用 Spot Testnet 金鑰。不要貼到聊天或 commit。正式帳戶需至少 100 USDT 可用資金。
3. 金鑰需開讀取、現貨交易和 IP 限制；關閉提款、內部／通用轉帳、槓桿、合約、選擇權、Portfolio Margin 和 FIX 交易權限。缺欄位或無法核對時拒絕放行。[Binance 權限 API 文件](https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/account)。
4. `ENABLE_LIVE_TRADING=false` 時仍可檢查帳戶。測試網驗收通過且決定真實交易後，才由操作者改為 `true`，執行 `bash scripts/live-control.sh start` 套用。
5. Mac 終端執行 `ssh -N -L 18080:127.0.0.1:8080 m31527@192.168.50.86` 並保持連線，瀏覽器開 `http://localhost:18080`。LAN HTTP 僅供觀察，頁面阻止透過未加密 LAN 送操作密碼。
6. 在正式帳戶區輸入伺服器 `config/live.env` 的 `LIVE_CONTROL_TOKEN`（不是交易所 secret），點「檢查正式 API 權限與餘額」。密碼只保留於當前輸入框，不寫入瀏覽器儲存空間。
7. 選買／賣與金額，產生交易預覽。預覽有效 120 秒，檢视數量、金額、方向，勾選績效審閱，輸入指定確認文字，按確認才下這一筆。

## 強制限制與適用範圍

- 獨立分配 100 USDT；單筆預估不超過 15、BTC 持倉預估不超過 30 USDT，只交易 BTCUSDT 現貨，不自動加碼。
- 每日虧損達 2、累計虧損達 5 USDT 停買；每日按 UTC 換日，累計停買不隨換日解除。每次準備／放行重新估值，沒有背景連續風控或自動止損。
- 每日最多 4 筆，兩筆間隔至少 30 分鐘。只賣本系統買入的 BTC，不使用帳戶既有 BTC。
- 市價單不保證成交價；15/30 是送單前限額，實際成交可能受滑價影響。
- 測試網須完成觀測時間、有效輪次、策略買賣成交、無待確認訂單及無停機等條件。績效審閱由本人於每次放行確認，程式不宣稱策略能獲利。
- API key 綁定帳本。外部買賣／存提影響 BTC 或 USDT 會導致餘額不一致，拒絕後續下單。建議獨立帳戶／子帳戶，關閉 BNB 抵扣（目前費用帳本只支援 BTC/USDT）。
- 每筆預覽 ID 只送一次：先記錄，再送交易所。逾時／重啟不重送；不依賴交易所對已成交 ID 的去重行為。

## 異常處理

「停止正式下單」設定持久停止標記，不等交易請求結束。已送到交易所的訂單仍可能成交，不會自動平倉。「查詢並補記待確認成交」只查原訂單、完整成交與費用、核對餘額，不送新單。對帳成功仍保留停止標記，人工核對後才能解除。

部分成交、非支援費用幣、外部帳戶異動、意圖保存但未送出、訂單不存在等情況都保留待確認，不提供刪帳本／重送捷徑。目前只自動入帳完整 FILLED 訂單。正式資料在 `data/live/live.db`，獨立於 Testnet/PAPER。

控制服務沒有對外 port，不將交易所金鑰載入 dashboard。Dashboard 代理固定路徑，所有寫入需操作密碼，沒有跨來源授權。反向代理需 HTTPS，禁止記錄 Authorization header。

## Testnet 已成交待確認修復

`bash scripts/testnet-soak.sh recover-pending` 僅針對既有 AmbiguousOrder、對應交易日誌及 SOAK_REVIEW_REQUIRED 停機。完整成交與餘額符合才補入，保留停機及歷史。確認後執行 `bash scripts/testnet-soak.sh resume-reconciled`，再 `bash scripts/testnet-soak.sh start`。

恢復後開新連續觀測窗口，保留全部舊資料、成交與錯誤。新窗口需再次完成策略買賣，不把修復前的成交算成新窗口無故障驗收。
