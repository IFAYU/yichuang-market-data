"""20/60/120/240 日均線乖離與估計買進價（合成資料）。"""
import importlib.util
import math
import pathlib

_p = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "build_etf_backtest.py"
_spec = importlib.util.spec_from_file_location("build_etf_backtest", _p)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_flat_history_every_window_ok_with_buy_price_equal_to_ma():
    rows = m.ma_deviation([100.0] * 300)
    assert [r["window"] for r in rows] == [20, 60, 120, 240]
    for r in rows:
        assert r["status"] == "OK" and r["ma"] == 100.0 and r["deviationPercent"] == 0.0
        assert r["dipPercent"] == 0.0 and r["buyPrice"] == 100.0 and r["moveToBuyPercent"] == 0.0


def test_short_history_says_so_per_window_and_invents_no_number():
    rows = {r["window"]: r for r in m.ma_deviation([100.0 + i for i in range(25)])}
    assert rows[60]["status"] == "NEED_MORE_HISTORY" and "ma" not in rows[60] and "buyPrice" not in rows[60]
    # MA20 of 105..124 = 114.5; 124/114.5-1 = 8.30%; only 6 daily deviations -> no dip yet
    assert rows[20]["status"] == "DIP_NEEDS_MORE_DAYS" and rows[20]["ma"] == 114.5
    assert rows[20]["deviationPercent"] == 8.3 and rows[20]["observations"] == 6 and "buyPrice" not in rows[20]


def test_matches_independent_brute_force():
    closes = [50 + 5 * math.sin(i / 7) + (-3 if i % 13 == 0 else 0) for i in range(300)]
    for r in m.ma_deviation(closes):
        n = r["window"]
        devs = []
        for t in range(n - 1, len(closes)):
            avg = sum(closes[t - n + 1:t + 1]) / n
            devs.append(closes[t] / avg - 1)
        srt = sorted(devs)
        k = int(len(srt) * 0.1)
        dip = sum(srt[:k]) / k
        ma = sum(closes[-n:]) / n
        assert r["observations"] == len(devs)
        assert abs(r["ma"] - ma) < 0.006 and abs(r["dipPercent"] - dip * 100) < 0.006
        assert r["buyPrice"] == m.to_tick(ma * (1 + dip)) and r["buyPrice"] < r["ma"]


def test_a_dip_above_the_average_is_capped_so_the_buy_price_never_exceeds_the_ma():
    rising = [50 + 0.2 * i for i in range(300)]  # a steady climb: even the lowest 10% deviations sit above the long average
    for r in m.ma_deviation(rising):
        assert r["status"] == "OK" and r["buyPrice"] <= r["ma"] + 0.05  # tick rounding only
        if r["dipCapped"]:
            assert r["dipPercent"] == 0.0 and r["buyPrice"] == m.to_tick(r["ma"])
    assert any(r["dipCapped"] for r in m.ma_deviation(rising))
    assert not any(r["dipCapped"] for r in m.ma_deviation([100.0] * 300))


def test_tick_rounding_and_no_ranking_fields():
    assert m.to_tick(34.2749) == 34.27 and m.to_tick(112.93) == 112.95 and m.to_tick(112.92) == 112.9
    keys = {k for r in m.ma_deviation([80 + (i % 9) for i in range(300)]) for k in r}
    assert not keys & {"rank", "score", "best", "recommend", "target"}


def test_analyze_carries_ma_deviation_on_split_adjusted_closes():
    # 0050-like: a 1-for-4 split in the middle must not leak into any average
    series = [(f"2024-01-{i:02d}" if False else f"d{i:04d}", (200.0 if i < 400 else 50.0) + (i % 5)) for i in range(800)]
    out = m.analyze(series)
    assert out["status"] == "OK" and out["splitAdjustments"]
    ma = {r["window"]: r for r in out["maDeviation"]}
    assert ma[240]["status"] == "OK" and 48 < ma[240]["ma"] < 56


import unittest


class MaDeviationTests(unittest.TestCase):
    def test_flat(self): test_flat_history_every_window_ok_with_buy_price_equal_to_ma()
    def test_short(self): test_short_history_says_so_per_window_and_invents_no_number()
    def test_brute_force(self): test_matches_independent_brute_force()
    def test_capped(self): test_a_dip_above_the_average_is_capped_so_the_buy_price_never_exceeds_the_ma()
    def test_tick_no_ranking(self): test_tick_rounding_and_no_ranking_fields()
    def test_analyze_split(self): test_analyze_carries_ma_deviation_on_split_adjusted_closes()


if __name__ == "__main__":
    unittest.main()
