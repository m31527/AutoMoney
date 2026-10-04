# 進場研究 v3：部署及下一批資料

## 這次變更

OpenTeddy 影子研究改用 `compact-entry-v3`：最多 40 根已收盤 K 線（1m 4 根、5m 8 根、15m 8 根、1h 20 根），OHLCV 使用陣列格式，提示詞加行情最多 10,000 bytes。這是輸入大小限制，不是假裝精確計算模型 token，也不能保證遠端沒有截斷。

明確標示可買／可賣與各自金額上限。模型必須回傳本次唯一識別碼、預算許可與固定 240 分鐘期限；bridge 核對回條、金額和期限。錯誤記為研究失敗，不納入有效建議。不會因為一直 HOLD 而强迫 BUY，不會降低風控門檻。

AutoMoney 核對 bridge 收到的提示詞／行情 SHA256。每筆紀錄保存實際精簡輸入、版本與回條驗證結果，供之後重播同一批案例。原始行情只使用決策當時可取得的已收盤資料；匯出後產生的未來報價不得放回模型輸入。

保持每小時一組兩次研究，兩种提示詞比較保留。原本 Ollama 模擬策略不變，所有研究仍然不下單。這不是歷史重播工具或本地／商業模型同期比較器；此版先建立其所需的可信輸入紀錄。

## 在 DGX 更新

把 `research-v3-update.tar.gz` 放入 `~/AutoMoney`，執行：

```bash
cd ~/AutoMoney
tar -xzf research-v3-update.tar.gz
python3 scripts/install-openteddy-bridge.py ~/OpenTeddy
cd ~/OpenTeddy
./openteddy service restart
cd ~/AutoMoney
bash scripts/openteddy-shadow.sh btc-eth
```

若原本 OpenTeddy 使用 system 服務，重啟加上 `--system`；手動／Docker 啟動則依原啟動方式重啟或重建。安裝器保留已有 token、模型和 URL 設定，不會切換付費模型。啟動腳本確認 bridge 版本至少為 2 才更新 ai-trader。

只有原本已在收集 SOL/XRP 實驗時，另外執行：

```bash
bash scripts/openteddy-shadow.sh sol-xrp
```

不要另開一套重複 BTC/ETH。既有資料卷、研究冷卻時間與 OpenTeddy 次數額度保留，重啟不會歸零。

## 驗收與匯出

部署健康檢查顯示 `version: 2` 只代表新版介面已載入，並不代表模型測試通過。第一組實際研究可能需等一小時原有冷卻到期。

下一份 ZIP 的 `summary.json → activity → ollama → shadow_ai` 應出現：

- `research_quality.policy_versions` 包含 `compact-entry-v3`。
- 成功新紀錄的 `provider_metadata.input_audit.receipt_verified` 為 true。
- 研究期限固定 240 分鐘；有 `research_input` 可以重播。
- 如果回條或預算理解不符，`research_quality.error_codes` 會列出錯誤，不能當成模型 HOLD。

初次更新後約 1–2 小時可匯出短區間，先確認輸入驗證通過，不必先等一整夜。通過後再收集 8–12 小時；最後 4 小時研究沒有完整後續報價是正常的。保留預篩與原風控，不根據本批結果自動改策略。

這次不是以成交次數驗收，而是先確認模型收到新輸入、欄位理解檢查通過、建議能與其後報價對照。回條通過仍不保證文字推理正確，也不證明獲利。

## 回復

使用先前 `openteddy-shadow-update.tar.gz` 還原 AutoMoney 原始碼及 bridge 模組，重新執行其安裝器、重啟 OpenTeddy 和兩組原啟動腳本。不要刪除資料卷。或執行 `scripts/ai-shadow-pair.sh btc-eth` 回復直接 Ollama 研究路徑（不是 v3）。
