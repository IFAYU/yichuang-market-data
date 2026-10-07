# Phase 3I.1 — 每日（交易日感知）市場資料發布：稽核、設計與分階段實作

狀態：**STAGED，未 commit、未 push、未 deploy、未啟用任何排程。** 現行 `weekly-publish.yml`（週日 10:00／14:00／20:00）原封不動。
`LEGACY_WEEKLY_POLICY = SUPERSEDED_PENDING_DAILY_MIGRATION`（產品決定已改為每日；2026-10-11 不再是必要的驗證關卡，歷史邏輯暫不刪除）。

> **3I.2 更新**：本文件是 3I.1 的稽核與設計。TWSE 目標改為 T+1 06:30 且只在早上查詢、「SUCCESS＝各市場達到自己的目標」、方案 C 已核准並實作，workflow 已建立在 `.github/workflows/`（僅手動）；以 `PHASE_3I_2_DAILY_INTEGRATION.md` 為準。

> 注意：本階段收到的指示在「22. CONCURRENCY」之後被截斷（沒有後續章節與最終報告格式）。本文件涵蓋第 0–21 節；第 22 節以下依第 21 節要求自行給出建議（§8）。

## 1. 基準（動手前）
| | HEAD | origin/main | 備註 |
|---|---|---|---|
| risk-profiler | `1489896` | `1489896` | 只有未追蹤檔（OTHER-SESSION：`.env.example`、`.vitest/`、`docs/`、`src/financial-planning/`；Phase 3H 未納入模組） |
| market-intelligence-tw | `5fbf6b9` | `5fbf6b9` | `data` 分支 `135a91e`；工作流程只有 `weekly-market-data`、`pages-build-deployment`，沒有進行中的執行 |

## 2. 稽核：每個「每週」假設（不只是 cron）
**market-intelligence-tw（管線）**
| 位置 | 每週假設 |
|---|---|
| `.github/workflows/weekly-publish.yml` | cron `0 2/6/12 * * 0`（週日 10/14/20 時）；concurrency `weekly-market-data` |
| `mitw/freshness.py` | `UPDATE_POLICY=WEEKLY`、`SCHEDULED_WEEKDAY`、`RETRY_TIMES`、`slot_at_or_before`、`window_close`（週一 00:00）、`missed_slots`、`classify()`（FRESH/STALE/SEVERELY_STALE＝錯過 0/1/2+ 個週更新）、`weekly_metadata()` |
| `mitw/runner.py` | 以「本週 slot 是否已發布」判斷 `WEEK_ALREADY_PUBLISHED`；`MAX_ATTEMPTS_PER_SLOT=3`（自 slot 起算）；`final_attempt` 才把一週算失敗 |
| `mitw/config.py` | `MAX_ATTEMPTS_PER_SLOT`、`MAX_ATTEMPTS_PER_DAY` 的「週」註解 |
| `mitw/runlog.py` | 狀態集 `SUCCESS/NOOP/WAITING/REFUSED/FAILED/SKIPPED`；`scheduledSlot`；`weekly_block()`（health.json 的 `weekly`）；`restore_from_health` 以「本週」重建嘗試次數 |
| `mitw/snapshot/gate.py` | **`market-date-same`：TWSE、TPEx 必須同一天**；`market-date-advances`：**兩個市場都要比上一版新**；`freshness-determinable` 用週度 `classify` |
| `mitw/snapshot/build.py` | manifest 寫入 `weekly_metadata`（updatePolicy、scheduledWeekday、retryTimes…）、`freshnessAtPublish.missedWeeklyUpdates`；`KEEP_RELEASES=14`（現為 14 週，改日更後只剩 14 天） |
| `scripts/check_weekly_run.py` | 以週日 slot 觀察一次執行 |
| `tests/` | `test_freshness.py`、`test_weekly_scenarios.py`、`weekly_vectors.json` |
| 休市日 | `closures=frozenset()`；註解寫明「不模擬台灣交易所假日」 |

**risk-profiler（App）**
`market-data/freshness.ts`（TS 雙胞胎，`FRESHNESS_POLICY.updatePolicy='WEEKLY'`，非 WEEKLY 一律 UNKNOWN）、`provenance.ts`（`describeFreshness`、`missedWeeklyUpdates`、「下次更新」）、`emergingFreshness.ts`（興櫃 manifest 也用同一個週度分類）、`health.ts`（`WeeklyHealth`）、`types.ts`（`MarketManifest.updatePolicy…` 註解）、`ui/market/shared.tsx`（「市場資料｜每週更新」「週日 10:00」）、`RelativeResults.tsx`（「每週更新」）、`DataHealthPage.tsx`（排程列「每週日」、「連續錯過的週更新」、說明段落、「每週快照公司數」）、測試 `freshness.test.ts`、`weeklyVectors.json`、`marketUi.test.tsx`、`marketData.test.ts`。
興櫃：`build_emerging_snapshot.py` 預設仍寫 `updatePolicy: "WEEKLY"`（App 的興櫃新鮮度依賴它）。

**一個遷移順序陷阱**：目前已上線的 App 看到 `updatePolicy ≠ WEEKLY` 會把整個市場資料判成 **UNKNOWN**。所以必須先部署「同時懂 WEEKLY 與 DAILY_TRADING_DAY」的 App，之後管線才能改發日更 manifest。

## 3. 官方資料可得性稽核（2026-10-07 23:10 實測；只用官方來源）
標記：`DOCUMENTED`＝官方文件寫明；`EMPIRICALLY_OBSERVED`＝本專案實測；`UNKNOWN`。

| 問題 | 結論 | 標記 |
|---|---|---|
| A 各端點揭露哪個日期？ | 每個資料列有 `Date`／`出表日期`（民國年）。TWSE 價格／本益比 `Date`＝交易日；TPEx 價格／本益比／興櫃行情 `Date`＝交易日；興櫃 `tpex_esb_highlight.Date` 是市場層級日期；財報類 `出表日期` 每天更新但內容期別（年度／季別）才是財報日期 | OBSERVED |
| B 代表「當日」交易嗎？ | TWSE `STOCK_DAY_ALL`、`BWIBBU_ALL`：交易日 D，**D+1 清晨才出現**（見 E）。TPEx 主板、興櫃：交易日 D 當天傍晚／晚間 | OBSERVED |
| C 官方有寫發布時間嗎？ | **沒有。** 兩個 swagger 的端點說明只有名稱（如「上市個股日成交資訊」「興櫃股票當日行情表」），沒有任何更新時間承諾 | DOCUMENTED（缺少） |
| D 21:00 時當日資料通常已有嗎？ | **TWSE：沒有**（兩次實測，D 日資料在 D+1 05:20 才產生）。**TPEx／興櫃：不一定**——2026-10-06 的 Last-Modified 是 18:00，2026-10-07 是 22:00；21:00 前不保證有，23:00 最保險 | OBSERVED（各 2 次；興櫃 1 次） |
| E 端點更新時間不同嗎？ | TWSE 價格／本益比 05:20:42／05:20:32（D+1）；TWSE 公司基本資料 05:24（D+1）；TPEx 價格 22:00:05、本益比 22:00:08、公司 22:02、EPS 22:00:40、月營收 22:05；興櫃行情 22:00:18、基本資料 22:02、資產負債表 22:07、損益表 22:08、月營收 22:09（皆 2026-10-07） | OBSERVED |
| F 價格與財報頻率不同？ | 是。價格每交易日；財報（年度／季別）只在公告後變動（目前 2026Q2）；月營收每月（目前 2026-08，出表 09-17） | OBSERVED |

證據來源：`data/raw/2026-10-06/*.meta.json` 的 `lastModified`（10-06 傍晚抓取）＋ 10-07 23:10 的即時回應標頭。TWSE 的「週末 D+1」（週五資料何時出現）**尚未觀察**。

**結論：21:00 不是 TWSE 當日資料的安全發布時間（資料根本還沒產生），對 TPEx／興櫃也不保證。** 這直接決定了 §5 的設計。

### 官方交易日曆
- TWSE OpenAPI `/holidaySchedule/holidaySchedule`（官方，「有價證券集中交易市場開（休）市日期」）：27 筆，涵蓋 2026 全年；含春節（2/12–2/20 休市，含「僅辦理結算交割」日）、國慶補假 **2026-10-09（週五）**、10-26（週一）等。名稱含「開始交易日／最後交易日」者是**交易日**，只是提及，不是休市（已處理）。
- TPEx 沒有獨立的官方日曆端點；興櫃與櫃買沿用集中市場交易日（**假設，未由文件證實**）。
- 颱風假等臨時休市不會預先出現在清單：必須用「確認過的額外休市日」（有紀錄的人工輸入）處理，否則該日會被視為應有資料而報 FAILED_NO_NEW_MARKET_DATA。這是殘餘風險。

## 4. 目標排程
Asia/Taipei 週一至週五 21:00（PRIMARY）、22:00（RETRY_1）、23:00（RETRY_2_FINAL）。台灣無日光節約（UTC+8），三個時間同一個星期幾：

| 本地 | UTC cron |
|---|---|
| 21:00 | `0 13 * * 1-5` |
| 22:00 | `0 14 * * 1-5` |
| 23:00 | `0 15 * * 1-5` |

GitHub 排程可能延遲啟動；決策用真實時鐘（`attempt_index`），不是看哪一條 cron 觸發。

## 5. 交易日感知設計（`mitw/trading_calendar.py`、`mitw/daily.py`；皆為純函式）
三個不可混用的概念：**排程日**（週一至週五）、**預期市場日**（某市場依其可得性模型在某時刻應已有的最新交易日）、**實際市場日**（來源實際帶的日期，不可由時鐘推斷）。

`target_date(market, as_of, calendar)`（即 `expectedLatestMarketDate`）：從 `as_of` 當天往回找，**第一個**「日曆為交易日，且 D＋offset 日的 expectedBy 時刻 ≤ as_of」的 D。TWSE：offset 1 天、06:00；TPEX／興櫃：offset 0、22:00。日曆回報 UNKNOWN 就回 `None`（不猜）。
來源優先序：官方日曆 → 人工確認的額外休市 → UNKNOWN（日曆未涵蓋的年度，平日不會被當成交易日）。

`expectedBy` 來自觀察，不是交易所保證；JSON 內每個市場都帶 `basis` 與 `evidence`。

### 週間邊界例子（2026）
- 週三 21:00：TWSE 目標＝週二；TPEx 目標＝週三（22:00 才算到期，21:00 先看看有沒有）。
- 週五 10-09 國慶補假：**不是 NO_TRADING_DAY**——TWSE 週四資料在週五 05:20 才出現，仍欠一次發布（補發）。補發後，後續休市日才是 NO_TRADING_DAY_EXPECTED。
- 週一 10-26 休市（接週末）：同理，補發 TWSE 週五資料。

## 6. 發布狀態模型
`SUCCESS`、`NOOP_ALREADY_PUBLISHED`、`WAITING_FOR_NEW_MARKET_DATA`、`NO_TRADING_DAY_EXPECTED`、`FAILED_NO_NEW_MARKET_DATA`、`FAILED_SOURCE`、`FAILED_VALIDATION`、`FAILED_PUBLICATION`，另增 **`SUCCESS_PARTIAL`**（不在你的最低清單內，需你確認）。

- 預檢（0 個請求）：所有市場的 Last Known Good ≥ 本窗口目標 → 今日為休市日 ⇒ `NO_TRADING_DAY_EXPECTED`，否則 ⇒ `NOOP_ALREADY_PUBLISHED`。日曆不明 ⇒ 仍會抓取並標 `CALENDAR_UNKNOWN`，**不會**當成假日。
- 抓取後（`evaluate`）：來源錯誤 ⇒ `FAILED_SOURCE`；日期格式錯／未來／早於 LKG／落在休市日 ⇒ `FAILED_VALIDATION`；沒有任何市場前進：尚欠 ⇒ 非最後一次 `WAITING`、最後一次 `FAILED_NO_NEW_MARKET_DATA`；全部達標 ⇒ `SUCCESS`；部分達標：非最後一次 `WAITING`（不發半成品）、最後一次 `SUCCESS_PARTIAL`（發布已前進的市場並點名落後者）。
- 已知假日**永遠不是失敗**。失敗時 LKG 完全不動，舊資料不會被當成新成功。人工觸發永遠不算最後一次。

## 7. 市場獨立性：TWSE／TPEx 是否同時前進？（需你決定）
**現況（已讀程式）**：一個 release、一個 manifest（原子發布）；gate 要求兩個市場**同一天**且**都比上一版新**，否則 WAITING（最後一次才算失敗）。
**問題**：在 21:00–23:00，TWSE 最新日期＝T−1、TPEx＝T（見 §3），所以「同一天」規則會讓**每天三次都失敗**（FAILED_NO_NEW_MARKET_DATA 變成常態）。這是需要改變語意的**確鑿正確性理由**，不是偏好。

| 方案 | 作法 | 後果 |
|---|---|---|
| A 維持「同一天」 | 改成隔日早上 06:00 後才發 | 保留完整原子語意，但違背「21:00–23:00 發布」的產品決定，且 TPEx 晚一天 |
| B 各市場獨立 release | 兩份 manifest | 拆散原子性、產業分布與比較池一致性、LKG 變複雜 |
| **C（建議）** | **仍是單一原子 release／manifest，但改成「每個市場各自達到自己的目標日」**；日期可相差（TWSE T−1、TPEx T） | manifest 本來就有逐市場 `marketAsOf`；App 已逐市場顯示日期；產業統計本來就是「每個數字保留自己的日期」；LKG 仍是一份整體。代價：頁面「上市與上櫃的資料日期不同」提示會天天出現（需調整文案，說明 TWSE 資料次日清晨產生）；P/E 分布混合 T−1 與 T 的價格 |

已實作為 `daily.evaluate`（方案 C）。**這是明確的語意變更，需你核准。** 另：成功條件要求「全部達標」才發，否則等重試；最後一次才發部分（避免一個晚到的交易所拖住另一個）。

## 8. 興櫃隔離、工作流程、並行（第 21、22 節的建議）
- 興櫃維持獨立命名空間 `/emerging/manifest.json`＋`/emerging/releases/<id>/emerging-snapshot.json`，不併入主 manifest；興櫃失敗不會阻擋 TWSE／TPEx，反之亦然。
- 建議**兩支獨立 workflow**（`proposed/daily-migration/`，**不在 `.github/`，未啟用**）：`weekday-market-publish.yml`（TWSE／TPEx）、`weekday-emerging-publish.yml`（興櫃）。理由：失敗網域分離、各自 concurrency、重試互不影響；重複的是發布步驟（約 30 行），可接受。不建議改名沿用舊 workflow（會混淆歷史紀錄與 `check_weekly_run`）。
- **並行**：兩支用**不同**的 concurrency group（`market-data-main`、`market-data-emerging`；若共用同一組，GitHub 只保留一個進行中＋一個等待，第三個會取消最舊的等待者，容易吃掉重試）。它們推的是 `data` 分支的**不相交路徑**，所以每次 push 前 `git pull --rebase origin data`，最多重試 3 次，只做 fast-forward，不 force。舊 workflow 的 push 沒有這個重試，遷移時主 workflow 也必須加上。
- 順序不變：不可變 release → 公開站讀回驗證（興櫃用 `scripts/verify_emerging_public.py`）→ **manifest 最後**。驗證失敗＝manifest 不動。
- 觀測：每次嘗試輸出一行 JSON 決策（state、reason、targets、marketDate）；寫入 `$GITHUB_STEP_SUMMARY`。

## 9. 興櫃每日語意與資料節奏
- 快照層級的市場日期取自市場彙總 `tpex_esb_highlight.Date`，並與每一列 `Date`、`RegisteredStocksNumber`（＝報價筆數）交叉檢查，任何不一致 ⇒ `FAILED_VALIDATION`。**單一公司當日無成交**（改用前日均價）**不影響**快照日期；`PREVIOUS_DAY_WEIGHTED_AVERAGE` 的日期仍是未知（不捏造）。
- 價格、財報（BVPS 期別 2026Q2）、月營收（2026-08）三種新鮮度各自獨立：價格日期 10-08 配 2026Q2 BVPS 是正常的。`classify_monthly_revenue` 以「每月 10 日＋5 日寬限」判斷。
- 冪等：每日模式的興櫃快照**不含時鐘**（`generatedAt` 移到 manifest），相同內容＝相同 release id＝NOOP（不重寫 manifest）。主快照內容含 `generatedAt`，所以以「市場日期未前進」擋下重複發布（現行 gate 已如此），不會產生無意義的新 release。
- release id＝內容 sha256 前 12 碼；每日節奏沒有碰撞風險（不同交易日內容必不同）；hash 驗證不變。

## 10. 新鮮度政策遷移（`classify_market` / `dailyFreshness.ts`）
- 以「最近一個**已關閉**的嘗試窗口（24:00 結束）本可發布的日期」為基準（`freshness_instant`）：FRESH＝已發布日期 ≥ 該目標；STALE＝落後 1 個**已完成交易日**；SEVERELY_STALE＝2 個以上；UNKNOWN＝日曆無法確定／日期缺漏或在未來。窗口開著時（21:00–24:00）不降級，只標 `updatePending`。
- 連假：沒有已完成的交易日，所以資料不會因日曆天數變舊（有測試）。
- 整體：全部最新＝FRESH；有新有舊＝PARTIAL；沒有 FRESH 且至少一個 STALE＝STALE；其他＝UNKNOWN。單一估值只看實際用到的市場（興櫃舊不影響純 TWSE／TPEx 估值，有測試）。
- 瀏覽器不呼叫任何假日 API：manifest 帶 `publicationPolicy`（排程、各市場可得性、官方休市清單與涵蓋年度），見 `daily.publication_policy()`。
- Data Health 用語（`freshnessLabels.ts`，未接上 UI）：最新／部分資料待更新／資料較舊／更新異常／休市．今日無需更新；內部狀態另存，不顯示英文代碼。

## 11. 遷移計畫（本階段不執行）
1. **M1（實作，無啟用）**：`mitw` 的 runner／gate／build／runlog／cli 接上 `daily.py`（`run-daily`、`SUCCESS_PARTIAL`、`publicationPolicy`、health schema 3、`check_daily_run.py`、`KEEP_RELEASES` 與興櫃 release 修剪）；App 改成**同時支援 WEEKLY 與 DAILY_TRADING_DAY**（依 manifest.updatePolicy 分派）、Data Health 改版、文案去除「每週／週日」；所有測試新增雙語料向量。
2. **M2**：部署支援雙政策的 App（此時管線仍是 WEEKLY，行為不變）。
3. **M3**：把兩支新 workflow 放進 `.github/workflows/`，**只有 `workflow_dispatch`（沒有 schedule）**，手動各跑一次，驗證決策、發布、讀回、manifest 最後；對照本文件的向量。
4. **M4（單一原子 commit）**：新 workflow 加上 cron，**同時**移除 `weekly-publish.yml` 的 `schedule:`（保留 `workflow_dispatch` 當手動後備）→ 避免兩套排程同時觸發（舊的週日 run 看到 DAILY manifest 會誤報失敗）。歷史 run 紀錄與 `health.json` 保留。
5. **M5**：觀察 5 個交易日，最好涵蓋一次休市補發（2026-10-09 國慶補假是第一個自然測試）；之後再決定刪除舊週度程式碼。

注意：2026-10-11（週日）的舊週度排程在 M4 之前仍會照常觸發；它會發布週四（10-08）的資料，是正常的資料更新，不再是驗證關卡。

## 12. 本階段新增／修改（全部未提交）
**market-intelligence-tw**：新增 `mitw/trading_calendar.py`、`mitw/daily.py`、`scripts/verify_emerging_public.py`、`tests/test_daily_policy.py`、`tests/test_emerging_builder.py`、`tests/daily_vectors.json`、`tests/fixtures/twse_holiday_schedule_2026.json`、`proposed/daily-migration/*.yml`、本文件；修改 `scripts/build_emerging_snapshot.py`（新增 `--daily` 模式；預設模式行為不變）。**沒有**修改任何每週管線檔案。
**risk-profiler**：新增 `market-data/dailyFreshness.ts`、`market-data/freshnessLabels.ts`、`__tests__/dailyFreshness.test.ts`、`__tests__/dailyVectors.json`（與 Python 端逐位元相同）。沒有任何現有檔案被修改，也沒有任何檔案 import 新模組（有測試鎖住）。

## 13. 殘餘風險
1. TWSE／TPEx 的發布時間只有 2 次觀察；週五→週六的 TWSE D+1 尚未觀察；TPEx 18:00 與 22:00 之間的更新節奏未知。**建議在 M3 用手動執行觀察至少一週再啟用排程。**
2. 方案 C 的語意變更（日期可不同）需核准；UI 文案要改。
3. 颱風假等臨時休市不在官方清單；沒有人工輸入前會被當成「該有資料」。
4. 官方清單只涵蓋 2026；2027 的平日在清單公布前一律 UNKNOWN。
5. GitHub 排程可能延遲／偶爾漏跑；60 天無活動的公開 repo 會自動停用排程（每日 commit 可避免）。
6. 資料庫膨脹：主快照約 4.2 MB（git 壓縮約 0.6 MB）× 約 250 次／年 ≈ 150 MB／年；興櫃 34 KB（gzip）／日。建議保留 14 份並定期以孤兒分支壓縮歷史。
7. 目前部署的 App 看到非 WEEKLY manifest 會判 UNKNOWN：**M2 必須先於 M4**。
