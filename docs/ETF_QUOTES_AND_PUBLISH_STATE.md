# ETF 收盤行情資源與發布狀態（Phase 8B，2026-10-12）

> 本文件只描述**本機已實作、以 fixture／真實唯讀樣本驗證**的內容。**沒有 push、沒有觸發任何正式發布、沒有新增排程。** 正式排程發布新 release 的成功路徑仍**未驗證**。

## 1. 為什麼 ETF 不放進公司快照
`market-snapshot.json` 的每一筆是「公司」（財報、EPS、本益比…）。ETF 沒有這些欄位，硬塞會讓既有 consumer（risk-profiler 的 Valuation Lab、hey 的投資轉接器）看到不是公司的東西。所以 ETF 是**獨立資源**：

```
etf/manifest.json                         ETF 資源自己的 latest 指標
etf/releases/<sha256[:12]>/etf-quotes.json   不可變 release
```
公司的 `manifest.json`、`releases/`、`health.json`、`SNAPSHOT_SCHEMA_VERSION` 一個字沒動（測試 `test_company_snapshot_files_and_contract_are_untouched`）。

## 2. 官方來源（唯讀 GET，兩個端點）
| 用途 | 端點 | 欄位 |
|---|---|---|
| 收盤價 | `https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL` | `Date`（民國 `1151008`）、`Code`、`ClosingPrice`（字串，元，TWD；無成交＝空字串）…共 1,380 列（含 ETF），全部同一個交易日 |
| 哪個代號是基金、哪一類 | `https://openapi.twse.com.tw/v1/opendata/t187ap47_L` | `基金代號`、`基金簡稱`、`基金類型`（11 種）… 271 列 |

**ETF 的判定來自官方基金主檔，不是代號長度或前綴**：價格檔有但主檔沒有的代號不是 ETF（`2330`、`0054` 之外的公司都不在輸出）。

## 3. 輸出契約（`schemaVersion 1.0.0`）
每檔：`symbol`、`market`（`TWSE`）、`instrumentType`（`ETF`）、`etfCategory`、`supportStatus`、`currency`、`price`（**十進位字串**）、`priceStatus`、`priceDate`、`retrievedAt`、`source`。文件層：`schemaVersion`、`priceDate`、`retrievedAt`、`generatedAt`、`source.evidence`（原始檔 sha256／位元組／列數／Last-Modified／ETag）、`source.terms`、`integrity`、`counts`。

- 無成交（空字串）＝`price: null`、`priceStatus: NO_CLOSE`；`0`、負數、非數字、超過 4 位小數＝`INVALID`；主檔有、價格檔沒有＝`NOT_QUOTED_BY_SOURCE`。**絕不轉成 0**，也不用買賣報價代替；輸出格式根本沒有 bid/ask 欄位。
- 價格日期不一致（同一檔案內多個交易日）、列數過少、必要欄位消失（schema drift）、重複代號、可報價的支援 ETF 少於 80% 有收盤價 → 拒絕建置，不發布。
- 公開 artifact 只含交易所公開事實，**沒有任何持倉、客戶或個資**（測試掃描）。

### 支援狀態（官方 `基金類型` → 狀態）
| 基金類型（官方用字） | 檔數 | 狀態 |
|---|---|---|
| 國內成分證券指數股票型基金 | 66 | **SUPPORTED** |
| 國內成分證券主動式交易所交易基金(股票) | 22 | **SUPPORTED** |
| 國外成分證券指數股票型基金（含債券 ETF） | 99 | NOT_SUPPORTED（海外成分；債券 ETF 在此類型內無法由類型區分，一併列為不支援） |
| 槓桿/反向指數股票型基金 | 51 | NOT_SUPPORTED |
| 國外成分證券主動式（股票）／（債券）、平衡型、境外、連結式、國外成份/加掛外幣、期貨信託 | 8＋3＋2＋3＋3＋6＋8 | NOT_SUPPORTED（外幣計價與期貨信託的價格不一定是 TWD） |
| 其他未知類型 | — | NOT_SUPPORTED（`UNKNOWN_FUND_TYPE`） |

不支援者仍在清單中、附原因、**不附價格**。主檔「國內成分」但簡稱含「債」者降級為 `BOND_SUSPECTED`。上櫃（TPEx）ETF 本版**未盤點**，不在資源內。

### 雜湊的意義
manifest 的 `files[...].sha256` **只證明內容一致性**（下載到的位元組＝發布時寫入的位元組）。它由同一個發布者計算，**不是**對來源的獨立認證。`contentSha256` 排除 `retrievedAt`／`generatedAt`，用來辨識「同一份來源資料重讀」。

## 4. 發布狀態：PUBLISHED / NO_UPDATE / FAILED（＋PENDING）
`mitw/publish_state.py`（純函式）。**日期沒前進本身不代表正常**；原因決定字眼：

| 情況 | 狀態 | reasonCode | normal |
|---|---|---|---|
| 寫入並驗證了新 release | PUBLISHED | `NEW_RELEASE` | ✓ |
| 來源日＝已發布日，且曆法證明沒有更新的交易日被欠（假日／週末／已發布） | NO_UPDATE | `NO_NEW_TRADING_DAY` | ✓ |
| 來源日＝已發布日，但依曆法應有更新的交易日 | 非最後一次嘗試 PENDING／最後一次 FAILED | `EXPECTED_TRADING_DAY_MISSING` | ✗ |
| 來源日比已發布日**早**（倒退） | FAILED（重試也不會變 pending） | `SOURCE_DATE_REGRESSED` | ✗ |
| 來源日在未來／無法解析／落在休市日 | FAILED | `SOURCE_DATE_IN_FUTURE`／`SOURCE_DATE_MALFORMED`／`SOURCE_ERROR` | ✗ |
| 抓取、結構、雜湊失敗 | FAILED | `SOURCE_ERROR`／`GATE_REFUSED` | ✗ |
| 曆法無法判斷（例如跨年份） | PENDING／FAILED，**絕不是 NO_UPDATE** | `CALENDAR_UNKNOWN` | ✗ |
| 同一交易日、內容不同 | FAILED（需要人看） | `SAME_DATE_DIFFERENT_CONTENT` | ✗ |

NO_UPDATE／FAILED／PENDING 都**不碰** Last Known Good（測試以位元組比對 manifest 與檔案清單）。

### 預期交易日
`mitw/daily.py` 的 `target_date("TWSE", now, calendar)`：交易日 D 的資料自 **D+1 06:30（Asia/Taipei）** 起算「應已存在」（`EMPIRICALLY_OBSERVED`，不是交易所保證）；曆法來自公司 manifest 的 `publicationPolicy.calendar`（證交所官方休市日），未涵蓋的年份＝UNKNOWN。

### 週排程（舊 runner）的修正
2026-10-11／12 的「FAILED」：週排程用「週一至週五」估算預期日（不含假日），把 10-09（國慶補假）當成缺漏的交易日，所以把一次完全正常的「沒有新交易日」標成本週失敗。現在 `mitw/runner.py` 讀 Last Known Good 的官方曆法：假日是休市日 → `NOOP`／`NO_UPDATE`（`NO_NEW_TRADING_DAY`），不累計失敗；曆法缺漏 → 仍 WAITING／FAILED（`CALENDAR_UNKNOWN`）；欠的交易日真的沒來 → FAILED；日期倒退 → 立即 FAILED。`health.json` 與每筆 run 紀錄新增 `publishStatus`、`publishReasonCode`（`latestOutcome`）。每日 runner 的狀態以 `outcome_of_status` 對應到同樣的字眼。

## 5. 本機驗證（fixture／唯讀樣本）
- `tests/test_etf_quotes.py`（23 項）：五檔（00919、00918、0056、00713、0050）原始列與轉換結果、主檔判定、不支援類型、空值／0／異常價格、來源壞掉拒絕、發布情境（正常交易日、週末、連假、公布前、倒退、未來、休市日、下載失敗、曆法未知、同日不同內容、公司檔案不受影響）。
- `tests/test_weekly_scenarios.py` 新增 4 項：假日週三次嘗試皆 NO_UPDATE；沒有曆法時不被當成正常；欠的交易日缺資料 → FAILED；來源倒退 → FAILED。
- 全套 `python -m unittest discover -s tests -t .`：268 項通過。
- 真實樣本：`tests/fixtures/etf_official_sample_2026-10-08.json` 為 2026-10-12 唯讀取得的官方原列（0050＝114.95、0056＝58.30→`58.3`、00713＝63.25、00918＝33.97、00919＝31.65；日期 `1151008`）。
- 以真實檔案本機建置：`scripts/build_etf_quotes.py --api <dir> --out <dir> --calendar-manifest <公司 manifest>` → `PUBLISHED`（資料日 2026-10-08），再跑一次 → `NO_UPDATE`。輸出只寫入指定的本機目錄。

## 6. 尚未做／未驗證
- **沒有 workflow、沒有 cron**，ETF 資源沒有被發布到公開站；hey 目前只能讀本機／測試用的 base URL。
- 正式排程「自動發布新 release」的成功路徑仍未被真實排程走過。
- 官方端點的再發布授權（政府資料開放授權條款第 1 版）對 `STOCK_DAY_ALL`、`t187ap47_L` 是否適用：**待確認（建議法務）**。條款不明前，不擴大公開範圍。
- 上櫃 ETF、債券／海外／槓桿／反向商品價格、ETF 淨值、持股、除息、歷史日價、調整後價：未做。
