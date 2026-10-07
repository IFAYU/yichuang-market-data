# Phase 3I.2 — 每日發布整合與手動驗證

狀態：程式與**僅手動觸發**的 workflow 已整合；**沒有任何新的 cron／schedule**。舊的 `weekly-publish.yml` 完全不動，仍是唯一的排程後備。
`LEGACY_WEEKLY_POLICY = SUPERSEDED_PENDING_DAILY_MIGRATION`（3I.3 才切換排程）。

## 1. 核心決定（本階段落實）
- **每個市場有自己的預期目標日**，TWSE 與 TPEx 不必同一天。
  - TWSE：交易日 T 的資料預期在 **T+1 06:30** 起可得（實測約 05:20，營運目標取 06:30）。
  - TPEx、興櫃：T 日 **21:00** 起欠 T（嘗試 21:00／22:00／23:00）。
  - 這些是**營運政策，依觀察而來，不是交易所的發布保證**；證據等級一律標 `EMPIRICALLY_OBSERVED`（manifest 與 health 內每個市場都帶 `availabilityBasis`、`evidence`）。
- **SUCCESS** ＝每個「此刻欠資料」的市場都達到**自己的**目標日（TWSE T−1＋TPEx T 是正常的 SUCCESS）。`SUCCESS_PARTIAL` 只在「至少一個欠資料的市場沒達到自己的目標」且已是最後一次嘗試時使用。
- 仍是**一個**原子 release、**一份**主 manifest。manifest 逐市場保存 `marketDate`、`expectedMarketDate`、`targetReached`、`carried`、`rawPartition`、`availabilityBasis`、`evidenceLevel`。
- 只查詢**欠資料**的市場：晚上不再問 TWSE（T−1 已發布）；06:30 不再問 TPEx。不欠的市場從 Last Known Good 的 raw partition **搬運**（保留原始抓取時間與 `reusedFrom`），搬運不到才改為抓取（`carryFallbackFetched`，只影響請求數，不影響正確性）。
- 休市日不等於零工作：2026-10-09（週五國慶補假）TWSE 仍欠週四資料，06:30 要補發；補發完成後才是 `NO_TRADING_DAY_EXPECTED`。

## 2. 交易日曆
`KNOWN_TRADING_DAY（TRADING）／KNOWN_NON_TRADING_DAY（CLOSED）／UNKNOWN`。官方 TWSE `holidaySchedule` 為權威來源（2026 全年）；2027 不虛構，平日一律 UNKNOWN → 失敗安全（不發布、不報告「確認的失敗」）。
**臨時休市覆寫**：`config/manual_closures.json`，每筆必須有 `date`、`markets`（TWSE／TPEX／EMERGING 或 ALL）、`reason`、`source`、`addedAt`；格式錯誤是**錯誤**，不會被靜默略過；沒有硬編任何颱風日。瀏覽器從 manifest 的 `publicationPolicy.calendar` 取得（含 `manualClosures`），不呼叫任何假日 API。

## 3. 新增模組（market-intelligence-tw）
`mitw/trading_calendar.py`、`mitw/daily.py`（純函式：目標日、precheck、evaluate、freshness）、`mitw/daily_ingest.py`（分市場抓取＋搬運＋raw 修剪）、`mitw/daily_runner.py`（`run-daily`）、`mitw/snapshot/daily_publish.py`（每日 gate＋原子發布）、`mitw/gitpublish.py`（資料分支安全推送）、`mitw/retention.py`、`scripts/verify_emerging_public.py`；`mitw/cli.py` 新增 `run-daily`、`push-paths`、`mark-verified`；`mitw/runlog.py` 認得每日狀態（`FAILED_*`、`SUCCESS_PARTIAL`，週度狀態行為不變）。週度程式碼（`runner.py`、`snapshot/build.py`、`snapshot/gate.py`、`freshness.py`、`pipeline.py`）**逐位元不變**。

## 4. Workflow（皆 workflow_dispatch only）
`weekday-market-publish.yml`、`weekday-emerging-publish.yml`。不同的 concurrency group；資料分支安全**不靠** concurrency：`push-paths` 先 fetch、只 stage 明確路徑、遠端移動時只在**與自己的命名空間不相交**才 rebase（`--autostash`），否則 fail closed；不 force；推送後用 `ls-remote` 驗證遠端 HEAD。順序：不可變 release → 公開站讀回驗證 → `mark-verified` → manifest 最後。`tests/test_workflows.py` 鎖住「沒有 schedule／cron」與「`weekly-publish.yml` 位元組不變」。

## 5. 保留政策（每日）
30 天內全留＋更早每週最新一份，再往前 12 週；**永遠保留** manifest 指向的 release 與前一份（回滾目標）；讀不到發布時間的 release 不刪。`KEEP_RELEASES=14` 只用於週度路徑。預期成長：主快照 git 壓縮約 0.6 MB／日（≈150 MB／年）、工作樹最多約 42 份；興櫃約 34 KB／日。

## 6. 觀測欄位
每次嘗試記錄：`businessTimeTaipei`、`attemptType`、每個市場的 `expectedMarketDate`／`actualSourceMarketDate`／`previousPublishedMarketDate`／`availabilityBasis`／`evidenceLevel`／`queried`／`carried`、`decision`、`releaseId`、`companyCount`、`sourceCounts`、`sourceHash`、`publicVerification`、`manifestUpdated`、`lastKnownGoodPreserved`；興櫃另有 `quoteCount`、`currentDayPriceCount`、`previousDayFallbackCount`、`bvpsCoverage`、`pbCoverage`。零請求結果（NOOP、休市）不改寫 `health.json`（不製造 Pages 變動）。

## 7. App（雙政策）
WEEKLY manifest 的判斷與畫面逐項不變（`classifyFreshness` 原樣）；`DAILY_TRADING_DAY` 走新規則；其他政策或無法讀取的 `publicationPolicy` → UNKNOWN。跨市場統計（產業 P/E 分布、子產業分布、同業 P/E、凍結的比較集）保留每家公司的市場日期，日期不同時顯示「上市與上櫃市場資料日期不同，本次同業估值使用各市場最新可取得資料。」；不裁掉較新的資料、不補較舊的、不沿用舊價。財報、月營收與價格的新鮮度各自獨立。

## 8. 遷移順序（已遵守）
App 先支援雙政策並部署（此時公開 manifest 仍是 WEEKLY）→ 手動觸發主 workflow（發布第一份 DAILY manifest）→ 驗證 → 手動觸發興櫃 workflow → UAT。**不啟用排程。** 3I.3 才會做排程切換：同一個 commit 加上 cron、移除舊週度 `schedule:`。
