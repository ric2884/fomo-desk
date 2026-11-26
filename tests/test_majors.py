import json
import math
import random

import httpx
import pytest

from desk.config import Settings, with_overrides
from desk.majors import backtest as mb
from desk.majors import data, live
from desk.majors.config import MajorsSettings
from desk.majors.engine import Engine
from desk.majors.strategies import Breakout, RollingExtreme, RSIRevert, Strategy, TrendEMA, TSMomentum

H = 3600
T0 = 1_609_459_200          # 2021-01-01 00:00 UTC
S = Settings()


def synth(hours, seed=1, start=T0, syms=None):
    syms = syms or MajorsSettings().symbols
    rnd = random.Random(seed)
    ts = [start + i * H for i in range(hours)]
    bars = {t: {} for t in ts}
    for sym in syms:
        p, drift = 100.0, rnd.uniform(-0.00003, 0.00006)
        for t in ts:
            o = p
            p *= math.exp(drift + rnd.gauss(0, 0.008))
            bars[t][sym] = (o, max(o, p) * 1.001, min(o, p) * 0.999, p)
    return ts, bars


class Fixed(Strategy):
    """Test strategy: follows a script {ts: target}."""
    family = "fixed"

    def __init__(self, script):
        super().__init__(True)
        self.script, self.t = script, 0

    def update(self, bar, side, held, ts):
        self.t = self.script.get(ts, self.t)
        return self.t


def one_coin(**kw):
    ms = MajorsSettings(symbols=["BTCUSDT"], **kw)
    return ms


def flat(n, px=100.0):
    return [(px, px, px, px)] * n


def feed(eng, prices, start=T0, sym="BTCUSDT"):
    for i, b in enumerate(prices):
        eng.on_bar(start + i * H, {sym: b})


# ---------- data ----------
def test_parse_klines_drops_unclosed_candle():
    rows = [[T0 * 1000, "1", "2", "0.5", "1.5", "10", (T0 + H) * 1000 - 1],
            [(T0 + H) * 1000, "1.5", "2", "1", "1.8", "5", (T0 + 2 * H) * 1000 - 1]]
    out = data.parse_klines(rows, now_ms=(T0 + H + 60) * 1000)
    assert out == [(T0, 1.0, 2.0, 0.5, 1.5, 10.0)]


def test_update_downloads_and_falls_back_on_blocked_funding(tmp_path):
    def handler(req):
        if "fundingRate" in str(req.url):
            return httpx.Response(451, json={"msg": "restricted location"})
        start = int(req.url.params["startTime"]) // 1000
        rows = [[(start + i * H) * 1000, "1", "1", "1", "1", "1", (start + (i + 1) * H) * 1000 - 1] for i in range(5)]
        return httpx.Response(200, json=rows)
    ms = MajorsSettings(db_path=str(tmp_path / "m.db"), symbols=["BTCUSDT"], start="2021-01-01")
    con = data.connect(ms.db_path)
    import desk.majors.data as d
    real_get = d._get
    d._get = lambda c, u, p: real_get(c, u, p) if "funding" not in u else (_ for _ in ()).throw(RuntimeError("451"))
    try:
        added = data.update(con, ms, httpx.Client(transport=httpx.MockTransport(handler)), now=T0 + 12 * H, sleep=0)
    finally:
        d._get = real_get
    assert added["BTCUSDT"] >= 5
    _, _, _, real = data.load(con, ["BTCUSDT"])
    assert real is False


# ---------- strategies ----------
def test_rolling_extreme_matches_brute_force():
    rnd = random.Random(3)
    vals = [rnd.random() for _ in range(300)]
    mx, mn = RollingExtreme(20, "max"), RollingExtreme(20, "min")
    for i, v in enumerate(vals):
        mx.push(v)
        mn.push(v)
        if i >= 19:
            assert mx.value == max(vals[i - 19:i + 1]) and mn.value == min(vals[i - 19:i + 1])


def run_strategy(st, closes, side_fn=lambda t: 0):
    out = []
    for i, c in enumerate(closes):
        out.append(st.update((c, c * 1.001, c * 0.999, c), side_fn(out[-1] if out else 0), 0, T0 + i * H))
    return out


def test_trend_long_up_short_down_flat_if_long_only():
    up = [100 * 1.001 ** i for i in range(300)]
    down = [100 * 0.999 ** i for i in range(300)]
    assert run_strategy(TrendEMA(20, 100, True), up)[-1] == 1
    assert run_strategy(TrendEMA(20, 100, True), down)[-1] == -1
    assert run_strategy(TrendEMA(20, 100, False), down)[-1] == 0
    assert run_strategy(TrendEMA(20, 100, True), up)[50] == 0          # warm-up: no signal yet


def test_breakout_and_exit():
    closes = [100.0] * 30 + [110.0] + [110.0] * 5 + [90.0]
    sig = run_strategy(Breakout(20, 10, True), closes, side_fn=lambda prev: prev)
    assert sig[30] == 1 and sig[-1] == -1


def test_rsi_buys_oversold_and_exits():
    closes = [100.0 + (i % 2) for i in range(30)] + [100 - 2 * i for i in range(1, 12)] + [100.0] * 5
    sig = run_strategy(RSIRevert(25, 75, 48, False), closes, side_fn=lambda prev: prev)
    assert 1 in sig[30:41] and sig[-1] == 0


def test_tsmom_only_decides_at_day_end():
    st = TSMomentum(24, True)
    out = run_strategy(st, [100 + i for i in range(80)])
    changes = [i for i in range(1, len(out)) if out[i] != out[i - 1]]
    assert all((T0 + i * H) % 86400 == 82800 for i in changes) and out[-1] == 1


# ---------- engine: costs, fills, risk ----------
def test_no_lookahead_signal_fills_next_open():
    ms = one_coin()
    eng = Engine(ms, S, {"BTCUSDT": Fixed({T0: 1})})
    eng.on_bar(T0, {"BTCUSDT": (100, 101, 99, 100)})
    assert not eng.pos and eng.pending == {"BTCUSDT": 1}
    eng.on_bar(T0 + H, {"BTCUSDT": (120, 121, 119, 120)})
    assert eng.pos["BTCUSDT"].entry == pytest.approx(120 * 1.0002)      # filled at the NEXT open


def test_round_trip_costs_fee_and_slippage():
    ms = one_coin()
    eng = Engine(ms, S, {"BTCUSDT": Fixed({T0: 1, T0 + 2 * H: 0})})
    feed(eng, flat(5))
    loss = S.bankroll_usd - eng.total
    assert loss == pytest.approx(100 * (2 * 0.0005 + 2 * 0.0002), rel=0.02)   # ≈ $0.14 on $100


def test_short_profits_when_price_falls():
    ms = one_coin()
    eng = Engine(ms, S, {"BTCUSDT": Fixed({T0: -1})})
    feed(eng, [(100, 100, 100, 100), (100, 100, 100, 100), (90, 90, 90, 90)])
    assert eng.total > 109


def test_funding_long_pays_short_receives():
    ms = one_coin()
    for side, sign in ((1, -1), (-1, 1)):
        eng = Engine(ms, with_overrides(S, day_lock_pct=9, day_bank_pct=9), {"BTCUSDT": Fixed({T0 + 6 * H: side})},
                     funding={"BTCUSDT": {T0 + 8 * H: 0.001}})
        feed(eng, flat(10))
        assert sign * eng.funding_paid < 0 and eng.funding_paid == pytest.approx(-sign * 100 * 0.001, rel=0.01)


def test_liquidation_at_2x():
    ms = one_coin(leverage=2.0)
    eng = Engine(ms, with_overrides(S, kill_switch_balance=-1), {"BTCUSDT": Fixed({T0: 1})})
    feed(eng, [(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 40, 45)])
    assert not eng.pos and eng.total < 2


def test_leverage_capped_at_2x():
    with pytest.raises(ValueError):
        MajorsSettings(leverage=3.0)


# ---------- engine: daily profit rule + money limits ----------
def test_day_rule_locks_then_banks():
    ms = one_coin()
    eng = Engine(ms, with_overrides(S, bank_mode="daily"), {"BTCUSDT": Fixed({T0: 1, T0 + 5 * H: 1})})
    prices = [(100, 100, 100, 100)] * 2 + [(101.5, 101.5, 101.5, 101.5)] + [(103, 103, 103, 103)] * 3
    ev = []
    for i, b in enumerate(prices):
        ev += eng.on_bar(T0 + i * H, {"BTCUSDT": b})
    assert any("target reached" in e for e in ev) and any("banked" in e for e in ev)
    assert eng.day_state == "banked" and eng.pos["BTCUSDT"].trail == ms.bank_trail
    feed(eng, [(103, 103, 100.5, 100.6)], start=T0 + 6 * H)            # trail hit → closed, profit banked
    assert "BTCUSDT" not in eng.pos and eng.banked > 0.5
    assert eng.banked == pytest.approx(eng.day_realized)           # all of today's realized profit is banked
    assert eng.tradable == pytest.approx(eng.total - eng.banked)


def test_locked_day_allows_closing_not_opening():
    ms = MajorsSettings(symbols=["BTCUSDT", "ETHUSDT"])
    eng = Engine(ms, S, {"BTCUSDT": Fixed({T0: 1}), "ETHUSDT": Fixed({T0 + 3 * H: 1})})
    for i, px in enumerate([100, 100, 104, 104, 104]):
        eng.on_bar(T0 + i * H, {"BTCUSDT": (px,) * 4, "ETHUSDT": (100,) * 4})
    assert eng.day_state != "normal" and "ETHUSDT" not in eng.pos


def test_kill_switch_closes_everything():
    ms = one_coin()
    eng = Engine(ms, S, {"BTCUSDT": Fixed({T0: 1})})
    feed(eng, [(100,) * 4, (100,) * 4, (30, 30, 30, 30), (30,) * 4])
    assert eng.killed and not eng.pos and not eng.pending


def test_state_round_trip():
    ms = one_coin()
    ts, bars = synth(300, syms=["BTCUSDT"])
    a = Engine(ms, S, {"BTCUSDT": TrendEMA(5, 20, True)})
    b_strat = TrendEMA(5, 20, True)
    for t in ts[:150]:
        a.on_bar(t, bars[t])
        b_strat.update(bars[t]["BTCUSDT"], 0, 0, t)
    b = Engine(ms, S, {"BTCUSDT": b_strat})
    b.restore(json.loads(json.dumps(a.state())))
    for t in ts[150:]:
        a.on_bar(t, bars[t])
        b.on_bar(t, bars[t])
    assert b.total == pytest.approx(a.total) and b.banked == pytest.approx(a.banked)


# ---------- walk-forward ----------
def test_walk_forward_report_and_no_peeking():
    ms = MajorsSettings(oos_start="2022-01-01", warm_bars=300)
    ts, bars = synth(int(24 * 365 * 1.6), seed=5)
    wf = mb.walk_forward(ts, bars, {}, ms, S)
    lines = mb.report(wf, ms, False)
    assert len(lines) <= 20 and lines[0].startswith("MAJORS") and lines[-1].startswith("VERDICT")
    assert "BTC buy & hold" in "\n".join(lines)
    # change prices AFTER the first fold starts: the first fold's choice must not change
    cut = mb.folds(ms, ts[-1])[0]
    bars2 = {t: ({k: (o * 3, h * 3, l * 3, c * 3) for k, (o, h, l, c) in b.items()} if t >= cut else b)
             for t, b in bars.items()}
    wf2 = mb.walk_forward(ts, bars2, {}, ms, S)
    for fam in wf["schedules"]:
        assert wf["schedules"][fam][0] == wf2["schedules"][fam][0]


# ---------- live paper == backtest ----------
def test_live_matches_backtest(tmp_path):
    ms = MajorsSettings(db_path=str(tmp_path / "m.db"), symbols=["BTCUSDT", "ETHUSDT"], warm_bars=200)
    ts, bars = synth(900, seed=8, syms=ms.symbols)
    con = data.connect(ms.db_path)
    k = 400
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,0)",
                    [(sym, t, *bars[t][sym]) for t in ts[:k + 1] for sym in ms.symbols])
    con.commit()
    choice = {"family": "trend", "params": [10, 50], "allow_short": True, "atr_k": 3.0, "leverage": 1.0}
    lv = live.LiveMajors(ms, S, choice, con)
    assert lv.engine.last_ts == ts[k]
    for j in range(k + 1, len(ts), 37):                       # new candles arrive in uneven chunks
        con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,0)",
                        [(sym, t, *bars[t][sym]) for t in ts[j:j + 37] for sym in ms.symbols])
        con.commit()
        lv.catch_up()
    lv2 = live.LiveMajors(ms, S, choice, con)                 # restart: state restored from file
    assert lv2.engine.total == pytest.approx(lv.engine.total)
    eng = mb.run(ts, bars, ms, S, {}, [(ts[k + 1], "trend", (10, 50), True, 3.0)], ts[k + 1], ts[-1])
    assert lv.engine.total == pytest.approx(eng.total)
    text = live.status_from_file(live.state_path(ms))
    assert "Gate:" in text and len(text.splitlines()) <= 12


# ---------- ATR stop-loss + weekly bank ----------
def test_atr_stop_starts_k_atrs_away_and_trails():
    ms = one_coin(atr_k=3.0)
    eng = Engine(ms, with_overrides(S, day_lock_pct=9), {"BTCUSDT": Fixed({T0 + 20 * H: 1})})
    feed(eng, [(100, 101, 99, 100)] * 22)                 # ATR ≈ 2 → stop ≈ entry − 6
    p = eng.pos["BTCUSDT"]
    assert p.stop == pytest.approx(p.entry - 3 * eng.atr["BTCUSDT"], rel=0.05)
    feed(eng, [(110, 111, 109, 110)] * 3, start=T0 + 22 * H)   # price rises → stop ratchets up
    assert eng.pos["BTCUSDT"].stop > 100
    feed(eng, [(110, 110, 95, 96)], start=T0 + 25 * H)       # drop through the stop → closed at the stop
    assert "BTCUSDT" not in eng.pos


def test_atr_off_means_no_stop():
    eng = Engine(one_coin(), with_overrides(S, day_lock_pct=9), {"BTCUSDT": Fixed({T0: 1})})
    feed(eng, [(100, 101, 99, 100)] * 5)
    assert eng.pos["BTCUSDT"].stop is None


def test_weekly_bank_in_engine():
    eng = Engine(one_coin(), with_overrides(S, day_lock_pct=9), {"BTCUSDT": Fixed({T0: 1})})
    mon = T0 + 3 * 86400                                   # 2021-01-04 is a Monday
    week = [(100,) * 4] * 2 + [(105,) * 4] * (24 * 7 - 2)
    feed(eng, [(100,) * 4] * 72 + week + [(105,) * 4] * 2)
    assert eng.banked == pytest.approx(5.0, rel=0.05)              # the week's +5% profit, banked
    assert "BTCUSDT" in eng.pos and eng.pos["BTCUSDT"].margin < 100  # position trimmed, not closed
    assert eng.tradable == pytest.approx(eng.total - eng.banked) and mon
