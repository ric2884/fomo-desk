import random

import pytest

from desk import backtest, tune
from desk import paper as paper_mod
from desk.config import Settings, with_overrides
from desk.ledger import Ledger
from desk.strategy import Position, Snap, Trade, find_entry
from tests.test_desk import paper_settings, path, synth_db

DAY = 86400.0
T0 = 1_700_006_400.0   # 00:00 UTC


# ---------- momentum entry ----------
def test_momentum_entry_buys_breakout_not_dip():
    s = with_overrides(Settings(), entry_type="momentum", mom_up=0.30, mom_window_s=120, min_age_s=120)
    flat_then_pump = [1.0] * 10 + [1.1, 1.2, 1.4, 1.5] + [1.5] * 5
    i = find_entry(path(flat_then_pump), 0.0, s)
    assert i == 12                       # 1.4 / price 120 s earlier (1.0) = +40%, new high
    dip = [2.0] * 9 + [1.6] * 10
    assert find_entry(path(dip), 0.0, s) is None
    assert find_entry(path(dip), 0.0, Settings()) is not None     # default is still the dip entry


def test_default_settings_unchanged():
    s = Settings()
    assert s.entry_type == "dip" and s.daily_loss_limit == 8 and s.kill_switch_balance == 60


# ---------- daily profit rule (ledger) ----------
def t(mint, entry, exit_, pnl):
    return Trade(mint, mint, entry, exit_, 1.0, 5.0, 5.0 + pnl, "x")


def test_day_rule_locks_at_1pct_and_banks_at_2pct():
    s = Settings()
    led = Ledger(s)
    led.on_open("a", T0 + 10, 5)
    led.on_close("a", T0 + 20, 6.2)                       # +$1.20 = +1.2%
    assert led.day_state(T0 + 30) == "locked"
    assert led.block_reason(T0 + 30, 5) == "day target reached"
    assert led.banked == 0
    led.on_open("b", T0 + 15, 5)                          # opened before the lock (allowed in a replay)
    led.on_close("b", T0 + 40, 6.0)                       # day now +$2.20 → bank it
    assert led.day_state(T0 + 50) == "banked" and led.banked == pytest.approx(2.2)
    assert led.total == pytest.approx(102.2) and led.equity == pytest.approx(100.0)
    assert led.block_reason(T0 + DAY + 10, 5) is None     # next day: trading again
    assert led.banked == pytest.approx(2.2)               # banked money stays banked


def test_day_rule_in_backtest_limits():
    s = Settings()
    trades = [t("a", T0 + 100, T0 + 200, 1.5), t("b", T0 + 300, T0 + 400, 1.0), t("c", T0 + DAY + 1, T0 + DAY + 9, 1.0)]
    res = backtest.apply_limits(trades, s)
    assert [x.mint for x in res["trades"]] == ["a", "c"]  # b blocked: day already +1.5%
    assert res["blocked"]["day target reached"] == 1


# ---------- daily rule in live paper ----------
def test_paper_day_rule_tightens_then_closes_losers(tmp_path):
    s = with_overrides(paper_settings(tmp_path), bankroll_usd=100, kill_switch_balance=60, max_exposure_frac=1.0)
    desk = paper_mod.PaperDesk(s)
    win = Position("W", "W", 0, Snap(T0, 1.0, 5e4), s)
    lose = Position("L", "L", 0, Snap(T0, 1.0, 5e4), s)
    win.last, lose.last = Snap(T0 + 60, 1.3, 5e4), Snap(T0 + 60, 0.9, 5e4)
    for p in (win, lose):
        desk.positions[p.trade.mint] = p
        desk.ledger.on_open(p.trade.mint, T0, 5.0)
    desk.ledger.day_pnl["2023-11-15"] = 0.0
    day = backtest.day_of(T0 + 100)
    desk.ledger.day_pnl[day] = 1.2                          # +1.2% → locked
    msgs = desk._day_rule(T0 + 100)
    assert any("target reached" in m for m in msgs)
    assert win.stop_px is not None and lose.stop_px is not None
    desk.ledger.day_pnl[day] = 2.5                          # +2.5% → banked
    msgs = desk._day_rule(T0 + 110)
    assert "L" not in desk.positions and "W" in desk.positions
    assert win.trail_override == s.day_lock_trail
    restored = Position.from_dict(win.to_dict(), s)
    assert restored.trail_override == s.day_lock_trail and restored.stop_px == win.stop_px


# ---------- walk-forward re-tune ----------
def test_grid_size_and_labels():
    g = tune.grid()
    assert len(g) == 108
    assert {o["entry_type"] for o in g} == {"dip", "momentum"}
    assert all(o["max_entry_age_s"] > o["min_age_s"] for o in g)
    assert tune.label(None) == "sit out"


def test_walk_forward_never_uses_the_test_day(tmp_path):
    s = with_overrides(Settings(), db_path=str(tmp_path / "w.db"))
    synth_db(s.db_path, n=400, days=6, seed=4)
    coins = backtest.load_coins(s)
    small = tune.grid()[::27]                                # 4 settings: fast
    wf = tune.walk_forward(coins, s, small)
    assert wf["test_days"] == wf["days"][tune.MIN_TRAIN_DAYS:]
    for tr in wf["oos"]["trades"]:
        assert backtest.day_of(tr.entry_ts) in wf["test_days"]
    lines = tune.report(wf, s, len(coins))
    assert len(lines) <= 20 and lines[-1].startswith("VERDICT")
    assert "NO PROFITABLE SETTINGS FOUND" in lines[-1]      # random walk + costs → no edge, honestly reported


def test_tune_passes_rule():
    assert not tune.passes({"trades": 29, "net": 50.0})
    assert not tune.passes({"trades": 40, "net": -0.01})
    assert tune.passes({"trades": 30, "net": 0.5})
