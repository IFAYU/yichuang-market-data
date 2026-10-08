# Phase 3I.2C — 切換前就緒檢查

- **PENDING_PUBLIC_READBACK 生命週期**：release 寫入（PENDING）→ 公開站讀回每個檔案的 sha256（VERIFIED，與 manifest 同一個 commit 發布）→ manifest 最後推送。新增 `reconcile-health`：若 live manifest 指向的 release 仍標 PENDING（舊版本或流程中斷留下），重新讀回公開位元組，成功才改成 VERIFIED；失敗只記錄檢查時間，維持 PENDING，不碰 release 與 manifest。無新發布的 run 會自動執行它。
- **週六清晨缺口（已修）**：舊設計的 TWSE 06:30 cron 為 `30 22 * * 0-4`（只到週五），週五交易日的資料（週六 06:30 起欠）要等到週一才發布。改為 `30 22 * * 0-5`，並有測試逐日檢查「每個交易日的晚間槽與隔日清晨槽都有 cron 命中」。**cron 尚未啟用。**
- **真實 CI 搬運（2026-10-08 手動 run 37708164401）**：TWSE 更新（2 個請求）、TPEx 從快取搬運（0 請求）；TPEx 893 家紀錄與前一 release 完全相同，TWSE 1095 家全數更新到 2026-10-07；release ae443e7fce16、公開讀回通過、health 為 VERIFIED。第二次 run（37708633942）為 NOOP_ALREADY_PUBLISHED、0 請求、沒有重複發布。
- **TPEx 更新＋TWSE 搬運方向**：僅有隔離 dry-run（`test_isolated_dry_run_tpex_update_with_twse_carry…`）與單元測試；`PRODUCTION_CARRY_FORWARD_NOT_YET_OBSERVED`（需 2026-10-08 晚上 21:00 之後的真實資料）。

- **週六重試（3I.2D，已實作、未啟用）**：`SATURDAY_RETRY_CRON_UTC = ("30 23 * * 5", "30 0 * * 6")` = 週六 07:30、08:30 台北。只有 TWSE 仍欠週五資料時才會發出請求；已補發時 precheck 回 NO_TRADING_DAY／NOOP，零請求。早晨嘗試永遠不宣告當日失敗；不影響 TPEx／興櫃與舊 weekly。
