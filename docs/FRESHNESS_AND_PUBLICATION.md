# Phase 3D.3 — Market Data Freshness & Automated Publication

目標：**系統可以舊，但絕對不能不知道自己舊。**（沒有部署、沒有 Phase 3E。）

## 1. Lifecycle audit（改動前的事實）

| 階段 | 3D.1 之前的狀況 | 風險 |
|---|---|---|
| 抓取 | 手動 `fetch` | 沒人跑就不更新 |
| 發布 | `out/latest` 整個資料夾被覆寫 | 中途失敗可能留下半新半舊 |
| 同步到 App | 手動 `npm run sync-market-data`，再重新部署 | 更新要重新部署 React |
| 新鮮度 | App 只看「日曆天 > 7」，且用瀏覽器本機日期 | 長假誤判、週末誤判、無法區分兩市場 |
| 失敗可見性 | 無 | 失敗了沒有人知道 |

## 2. Weekly update policy（Phase 3D.3A，取代 3D.3 的「每天」）

產品是估值工具，不是看盤工具。`MARKET_DATA_UPDATE_POLICY = WEEKLY`：**每週日 10:00（Asia/Taipei）更新一次**，抓 TWSE / TPEx 最近可取得的完整交易日（通常是週五）。**更新日 ≠ 資料日**：週日成功發布不會把市場日期寫成週日；每個數字保留自己的市場日期。

manifest 1.4.0 欄位：`updatePolicy, timezone, scheduledWeekday, scheduledTime, retryTimes, lastSuccessfulPublication, nextScheduledPublication, marketAsOf{TWSE,TPEX}, publishedAt`，以及原本的 release / sourceFetchedAt / generatedAt / sourceHash / files[].path / gate。**新鮮度由這些欄位加現在時間計算，不看 generatedAt。**

## 3. Freshness = 是否錯過應有的週更新

- 每個「排程時段」是一個週日 10:00。時段的重試窗口到當天午夜（週一 00:00）結束。
- 窗口結束時，若上次成功發布早於該時段 → 該時段「錯過」。
- 0 次錯過 FRESH（包含「這週的更新還在重試」）；1 次 STALE；2 次以上 SEVERELY_STALE。
- UNKNOWN：政策不是 WEEKLY、發布時間缺漏／無法解析／在未來、市場日期缺漏／無法解析／在未來或晚於發布日。UNKNOWN 不會被當成正常。
- `marketAgeTradingDays`（價格落後幾個交易日）只作參考，**不能單獨把正常的週快照判成 STALE**。關鍵回歸：週五使用上週日發布的資料仍是 FRESH。
- 財報另外判斷（法定公告期限 + 7 天寬限）；沒有新季度財報不是失敗。
- 限制：交易所國定假日未內建，只影響上面那個參考用的 market age。
- Python（`mitw/freshness.py`）與 TypeScript（`freshness.ts`）共用同一份手算向量 `weekly_vectors.json`，兩邊測試強制內容一致。

### 排程行為與重試
週日 10:00 → 14:00 → 20:00，最多 3 次，成功後同週後續執行不碰外部（NOOP，0 個請求）；3 次都沒成功 → 保留上一版、health 記錄失敗，週一 00:00 起顯示 STALE。不使用 proxy / IP 輪替 / 瀏覽器爬取 / captcha 繞過 / 隨機 User-Agent。

### 市場日期必須前進（「排程成功 ≠ 資料真的更新」）
發布閘門 `market-date-advances`：新的 marketAsOf 必須**大於**上一版。沒前進＝`NO_NEW_MARKET_DATA`：前兩次嘗試記為 WAITING 等下一次重試；當週最後一次嘗試記為 FAILED（本週發布失敗）、保留 Last Known Good。維護者重建同一市場日期需明確旗標 `--republish`（會寫入 gate.exceptions）。

## 4. 自動更新架構比較

| | A. GitHub Actions + 靜態資料 | B. Zeabur 排程 + storage | C. 其他（本機排程） |
|---|---|---|---|
| 費用 | 免費額度足夠 | volume/排程需付費 | 免費但依賴本機開機 |
| 可靠度 | 高（獨立於 App） | 中 | 低（睡眠/關機） |
| 部署複雜度 | 低 | 中高 | 低 |
| 回滾 | 重指 manifest（releases 不可變） | 同左但需自管 | 同左 |
| 歷史 | git／artifact | 需自管 | 本機 |
| 取資料延遲／CORS | 靜態主機（需 CORS `*`） | 同源 | 無法對外 |
| 額外伺服器 | 否 | 是 | 否 |

**決定：A（Phase 3D.4 已定案）。** 獨立公開 repo `yichuang-market-data`：`main` 放 pipeline 程式、測試、文件與 GitHub Actions；`data` 分支只放發布內容（`manifest.json`、`health.json`、`releases/<id>/…`），由 GitHub Pages 提供（CORS `*`）。App 讀 `MARKET_DATA_BASE_URL`，更新**不需要重新部署 React**。整個流程**沒有任何 secret**：workflow 只用內建 `GITHUB_TOKEN` 推 `data` 分支。

### 發布順序（工作流程）
1. 在 runner 上還原 `data` 分支到 `out/`，從已發布的 `health.json` 還原本週嘗試次數（runner 是一次性的）。
2. `mitw run`：抓取 → 驗證 → 閘門 → 寫入不可變 release → 本機最後才換 `manifest.json`。
3. 先只推 release（沒人指向它）→ `verify-remote` 輪詢公開網址，直到每個檔案的 sha256 與 manifest 記錄一致 → 才推 `manifest.json` 與 `health.json`。
4. 讀不到：還原 manifest、把本次紀錄改寫成 FAILED（`REMOTE_NOT_READABLE`）、只推 health、工作失敗。上線中的 manifest 全程不變。

## 5. 原子發布與 Last Known Good

1. `releases/<sourceHash[:12]>/` 寫入 market-snapshot.json、industry-snapshots.json、release.json，並重新讀回驗證雜湊。
2. 最後才原子替換 `manifest.json`（唯一的「最新」指標）。App 只讀 manifest → 同一 release 的兩個檔。
3. 任何失敗（抓取、schema、gate）：**不動**上一個 release 與 manifest。資料相同 → `UNCHANGED`，不寫入。
4. 回滾：`mitw rollback <release>`，先重新驗證再重指 manifest。
5. Availability ≠ Freshness：有 Last Known Good 只代表「有資料可用」，是否新鮮仍由 marketAsOf 決定，App 會照實顯示。

## 6. Production gate（拒絕發布的條件）

schema 版本、覆蓋率、離群值方法學（outlierPolicy、Tukey 圍籬、百分位定義、OFFICIAL_TTM）、市場日期存在、兩市場日期相同（例外須明確旗標並記錄）、**日期不得早於目前最新版**、新鮮度可判定。App 建置時（prebuild）再驗一次，並對過舊資料印出提醒（不擋建置）。

## 7. 更新紀錄與 Data Health

每次 `run` 寫一筆 run record（runId、起訖、來源、HTTP 狀態、筆數、schema 驗證、市場日期、發布結果、失敗原因），保留最近 30 筆；`health.json` 彙整最近成功／最近失敗／連續失敗次數。`health.json` **只供顯示，不參與新鮮度判斷**。`/valuation/data-health` 顯示快照事實、新鮮度與更新紀錄；health.json 缺少時明說「沒有更新紀錄」，新鮮度不受影響。

## 8. UI 行為

- 公開／Pre-IPO 頁頂：橫幅（原文字串）＋ 市場資料 上市/上櫃日期 ＋ 財務資料 YYYY Qx。UNKNOWN 不假裝正常。
- 公司層：股價日期、官方 P/E 日期、產業統計「統計基準 YYYY/MM/DD–YYYY/MM/DD」。
- 結果頁：非 FRESH 時，結果區塊頂端**持續**顯示警告；每個估值區間標示「依 YYYY-MM-DD 市場倍數計算」（Pre-IPO 的潛在價值／上漲空間／MOIC／IRR 同區塊）。信心分數依狀態扣分（STALE −15、SEVERE/UNKNOWN −30）。

## 9. Migration plan

1. （已做）mitw 新版 `out/` 結構；App `public/market-data` 重新同步，舊平面檔移除。
2. 用戶審閱本階段。
3. 決定資料發布位置（見第 4 節）；若選 A，啟用 workflow 的 schedule、App 把 market-data 基底改為外部 URL（需同步更新 CSP/連線稽核與 boundary 測試的「一個同源 fetch」限制）。
4. 之後部署 React App（本階段**未部署**）。
