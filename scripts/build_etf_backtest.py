"""ETF 歷史回測情境表（內部情境試算，非買進建議）。

資料：TWSE 官方 STOCK_DAY 逐月日成交（未還原權息的收盤價）。
方法：每個交易日只用「當日以前」252 個交易日的收盤算 P10/P20/P30 價位（無前視）；
      當日收盤 <= 該 P20 即視為「落入低檔區」，統計其後 120 個交易日的價格報酬。
限制：價格報酬未含配息、價位未還原除息缺口（偏保守）；樣本重疊、不得當作預測。
用法：python build_etf_backtest.py --out <json路徑> [--symbols 0050,0056,...] [--months 40]
"""
import argparse, json, statistics, sys, time, urllib.request
from datetime import date, datetime, timezone

SYMBOLS = ["0050", "0056", "00713", "00918", "00919"]
LOOKBACK, FORWARD = 252, 120


def month_starts(n):
    y, m = date.today().year, date.today().month
    out = []
    for _ in range(n):
        out.append(date(y, m, 1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def fetch_month(sym, d):
    url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={d:%Y%m%d}&stockNo={sym}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                j = json.load(r)
            if j.get("stat") != "OK":
                return []
            rows = []
            for row in j["data"]:
                y, m, dd = row[0].split("/")
                try:
                    close = float(row[6].replace(",", ""))
                except ValueError:
                    continue
                rows.append((f"{int(y) + 1911}-{m}-{dd}", close))
            return rows
        except Exception:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"抓取失敗 {sym} {d}")


def pct(sorted_vals, p):
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def adjust_splits(series):
    """單日跌幅>45%且比值接近整數倍(2~10)視為分割，把之前的價格除以倍數。ETF 單日不可能真跌這麼多。"""
    closes = [c for _, c in series]
    splits = []
    for i in range(len(closes) - 1, 0, -1):
        ratio = closes[i - 1] / closes[i]
        if ratio > 1.8 and abs(ratio - round(ratio)) < 0.15 * round(ratio):
            k = round(ratio)
            splits.append({"date": series[i][0], "ratio": k})
            closes[:i] = [c / k for c in closes[:i]]
    return closes, splits[::-1]


def analyze(series):
    closes, splits = adjust_splits(series)
    if len(closes) < LOOKBACK + FORWARD + 20:
        return {"status": "INSUFFICIENT_HISTORY", "tradingDays": len(closes)}
    zone_days, rets = 0, []
    for i in range(LOOKBACK, len(closes) - FORWARD):
        p20 = pct(sorted(closes[i - LOOKBACK:i]), 0.2)
        if closes[i] <= p20:
            zone_days += 1
            rets.append(closes[i + FORWARD] / closes[i] - 1)
    win = sorted(closes[-LOOKBACK:])
    cur = closes[-1]
    out = {
        "status": "OK",
        "tradingDays": len(closes),
        "from": series[0][0], "to": series[-1][0],
        "lastClose": cur,
        "splitAdjustments": splits,
        "window252": {"p10": round(pct(win, .1), 2), "p20": round(pct(win, .2), 2),
                      "p30": round(pct(win, .3), 2), "median": round(pct(win, .5), 2),
                      "high": win[-1], "low": win[0]},
        "currentPercentile": round(sum(1 for v in win if v <= cur) / len(win), 3),
        "backtest": {"rule": "收盤<=前252日P20，持有120交易日（價格報酬，未含配息）",
                     "signalDays": zone_days, "samples": len(rets)},
    }
    if len(rets) >= 20:
        rs = sorted(rets)
        out["backtest"].update({
            "winRate": round(sum(1 for r in rets if r > 0) / len(rets), 3),
            "medianReturn": round(statistics.median(rets), 4),
            "p10Return": round(pct(rs, .1), 4), "p90Return": round(pct(rs, .9), 4),
            "worstReturn": round(rs[0], 4)})
    else:
        out["backtest"]["note"] = "樣本不足（<20），不顯示統計"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--months", type=int, default=40)
    a = ap.parse_args()
    result = {}
    for sym in a.symbols.split(","):
        series = []
        for d in month_starts(a.months):
            series += fetch_month(sym, d)
            time.sleep(1.2)
        series = sorted(set(series))
        result[sym] = analyze(series)
        print(sym, result[sym].get("status"), result[sym].get("tradingDays"), file=sys.stderr)
    doc = {"schemaVersion": 1, "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "source": "TWSE exchangeReport/STOCK_DAY（未還原權息）",
           "disclaimer": "歷史情境試算，非買進建議；價格報酬未含配息，樣本重疊，過去不代表未來。",
           "etfs": result}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
