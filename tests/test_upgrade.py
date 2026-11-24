import asyncio
import copy
import random

import pytest

from desk import audit, backtest, compare, missed, realcosts
from desk import paper as paper_mod
from desk.config import Settings, with_overrides
from desk.market import MarketMode
from desk.strategy import Snap, Trade
from desk.upgrade import UpgradedPaperDesk, extra_checks
from desk.upgrade_settings import UpgradeSettings
from tests.test_desk import paper_settings, synth_db

POOL, VAULT, DEV = "P" * 44, "V" * 44, "D" * 44
# Shape copied from the real report seen with --rugshape (docs/api-examples.md)
CLEAN = {
    "mintAuthority": None, "freezeAuthority": None, "creator": DEV, "rugged": False,
    "graphInsidersDetected": 0, "insiderNetworks": [],
    "markets": [{"pubkey": POOL, "liquidityA": VAULT, "liquidityB": "W" * 44,
                 "liquidityAAccount": {"mint": "M" * 44, "owner": POOL, "delegate": None},
                 "liquidityBAccount": {"mint": "S" * 44, "owner": POOL, "delegate": None},
                 "lp": {"lpLockedPct": 100}}],
    "topHolders": [{"address": VAULT, "owner": POOL, "pct": 70.0, "insider": False},
                   {"address": "a" * 44, "owner": DEV, "pct": 2.0, "insider": False},
                   {"address": "b" * 44, "owner": "x" * 44, "pct": 6.0, "insider": False}],
    "risks": [],
}
U = UpgradeSettings()


def layers(raw, age=600, u=U):
    return {layer.name: layer.result for layer in audit.audit(raw, age, u)}


def bad(**changes):
    r = copy.deepcopy(CLEAN)
    for k, v in changes.items():
        r[k] = v
    return r


# ---------- (1) 8-layer audit ----------
def test_audit_clean_passes_all_8():
    res = layers(CLEAN)
    assert list(res) == audit.LAYERS and set(res.values()) == {"pass"}
    assert audit.audit_reason(audit.audit(CLEAN, 600, U), U) is None


@pytest.mark.parametrize("raw,age,layer", [
    (bad(mintAuthority="X" * 44), 600, "authorities"),
    (bad(freezeAuthority="X" * 44), 600, "authorities"),
    (bad(topHolders=CLEAN["topHolders"][:1] + [{"address": "a" * 44, "owner": DEV, "pct": 8.0}]), 600, "dev holding"),
    (bad(topHolders=CLEAN["topHolders"] + [{"address": "c" * 44, "owner": "y" * 44, "pct": 30.0}]), 600, "top10"),
    (bad(markets=[{**CLEAN["markets"][0], "lp": {"lpLockedPct": 40}}]), 600, "lp locked"),
    (CLEAN, 60, "pool age"),
    (bad(graphInsidersDetected=7), 600, "linked wallets"),
    (bad(insiderNetworks=[{"id": "n", "size": 25}]), 600, "linked wallets"),
    (bad(topHolders=CLEAN["topHolders"] + [{"address": "c" * 44, "owner": "z" * 44, "pct": 12.0, "insider": True}]),
     600, "insiders"),
    (bad(markets=[{**CLEAN["markets"][0], "liquidityAAccount": {"owner": POOL, "delegate": "E" * 44}}]), 600, "delegations"),
    (bad(risks=[{"name": "Token account delegated", "level": "warn"}]), 600, "delegations"),
])
def test_audit_each_layer_can_fail(raw, age, layer):
    res = layers(raw, age)
    assert res[layer] == "fail"
    assert audit.audit_reason(audit.audit(raw, age, U), U).startswith(f"audit: {layer}")


def test_audit_unknown_layers():
    raw = {k: v for k, v in CLEAN.items() if k not in ("graphInsidersDetected", "insiderNetworks")}
    assert layers(raw)["linked wallets"] == "unknown"
    assert audit.audit_reason(audit.audit(raw, 600, U), U) is None                       # lets through
    strict = UpgradeSettings(audit_unknown_blocks=True)
    assert audit.audit_reason(audit.audit(raw, 600, strict), strict).startswith("audit: linked wallets")
    assert set(layers(None).values()) == {"unknown"}


def test_audit_survives_junk():
    junk = {"markets": "x", "topHolders": [1, None], "risks": ["a"], "insiderNetworks": "?"}
    assert len(audit.audit(junk, 600, U)) == 8


# ---------- (2) market mode ----------
def market_with(btc_path, sol_path, step=60):
    mm = MarketMode(U)
    for i, (b, s) in enumerate(zip(btc_path, sol_path)):
        mm.add(i * step, b, s)
    return mm


def test_market_calm_allows():
    mm = market_with([100] * 120, [10] * 120)
    assert mm.blocked(119 * 60) is None


def test_market_btc_fast_drop_blocks_then_cools_off():
    btc = [100] * 60 + [100 - i * 0.2 for i in range(10)]          # −1.8% in 10 min
    mm = market_with(btc, [10] * 70)
    reason = mm.blocked(69 * 60)
    assert reason and reason.startswith("market: BTC")
    assert mm.blocked(69 * 60 + 1800) is not None                   # still cooling off
    assert mm.blocked(69 * 60 + 3601) is None


def test_market_sol_1h_drop_blocks():
    sol = [10 - i * 0.01 for i in range(60)]                          # −5.9% over an hour, slowly
    mm = market_with([100] * 60, sol)
    assert mm.blocked(59 * 60).startswith("market: SOL")


def test_market_no_data_fails_open():
    mm = MarketMode(U)
    assert mm.blocked(1000) is None and mm.stale(1000)


# ---------- (4) realistic costs ----------
def trade(legs, proceeds=6.0):
    return Trade("M", "X", 0, legs[-1][0], 1.0, 5.0, proceeds, "stop", legs=legs)


def test_realcosts_zero_settings_cost_nothing():
    z = UpgradeSettings(network_fee_usd=0, mev_buy_pct=0, fast_move_factor=0)
    assert realcosts.extra_cost(trade([(100, "stop", 6.0)]), [], z) == 0


def test_realcosts_network_mev_and_fast_dump():
    calm = [Snap(t, 1.0, 5e4) for t in range(0, 200, 15)]
    dump = calm[:-1] + [Snap(195, 0.8, 5e4)]                          # −20% in the last 15 s
    t = trade([(60, "take profit", 3.0), (195, "stop", 3.0)])
    c_calm = realcosts.extra_cost(t, calm, U)
    assert c_calm == pytest.approx(3 * 0.02 + 6.0 * 0.005)
    c_dump = realcosts.extra_cost(t, dump, U)
    assert c_dump == pytest.approx(c_calm + 3.0 * 0.05)              # capped at 5%
    realcosts.apply(t, dump, U)
    assert t.proceeds_usd == pytest.approx(6.0 - c_dump)


# ---------- (3) missed check ----------
def test_missed_check_6h_later(tmp_path):
    from desk import db
    con = db.connect(tmp_path / "m.db")
    missed.record(con, "WIN", "WIN", 0, "audit: insiders 15%", 1.0)
    missed.record(con, "DEAD", "DEAD", 0, "top10 40%", 1.0)
    missed.record(con, "NEW", "NEW", 20_000, "top10 40%", 1.0)
    assert missed.due(con, 6 * 3600 - 1, U) == []
    assert set(missed.due(con, 6 * 3600 + 1, U)) == {"WIN", "DEAD"}

    async def fake_fetch(mints):
        return [{"dexId": "pumpswap", "baseToken": {"address": "WIN"}, "priceUsd": "3.0", "liquidity": {"usd": 9e4}}]
    assert asyncio.run(missed.run_checks(con, 6 * 3600 + 1, U, fake_fetch)) == 2
    lines = missed.summary(con, U)
    assert len(lines) <= 20 and "Missed winners (≥ +50%): 1" in lines[1] and "dead/−90%: 1" in lines[1]
    assert "1 waiting" in lines[0]


# ---------- plugged into paper trading ----------
LOOSE = UpgradeSettings(audit_min_pool_age_s=0, audit_max_top10_pct=100, audit_max_dev_pct=100,
                        network_fee_usd=0, mev_buy_pct=0, fast_move_factor=0)


def run_desk(desk, coins, extra_s=3700):
    now, end = min(c.t0 for c in coins), max(c.t0 for c in coins) + extra_s
    while now < end:
        desk.step(now)
        now += 5
    return {t.mint: round(t.pnl, 6) for t in paper_mod.load_trades(desk.con)}


def test_upgraded_desk_with_loose_settings_equals_old_desk(tmp_path):
    s_old, s_new = paper_settings(tmp_path / "a"), paper_settings(tmp_path / "b")
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    for s in (s_old, s_new):
        synth_db(s.db_path, n=60, days=0.15, seed=11)
    coins = backtest.load_coins(s_old)
    old = run_desk(paper_mod.PaperDesk(s_old, rng=random.Random(0)), coins)
    new = run_desk(UpgradedPaperDesk(s_new, u=LOOSE, rng=random.Random(0)), coins)
    assert old and new == old


def test_upgraded_desk_blocks_and_records_missed(tmp_path):
    s = paper_settings(tmp_path)
    synth_db(s.db_path, n=60, days=0.15, seed=11)
    coins = backtest.load_coins(s)
    strict = UpgradeSettings(audit_min_pool_age_s=10_000)            # every buy fails "pool age"
    desk = UpgradedPaperDesk(s, u=strict, rng=random.Random(0))
    assert run_desk(desk, coins) == {}
    reasons = [r[0] for r in desk.con.execute("SELECT reason FROM missed")]
    assert any(r.startswith("audit: pool age") for r in reasons)


def test_upgraded_desk_market_crash_blocks(tmp_path):
    s = paper_settings(tmp_path)
    synth_db(s.db_path, n=60, days=0.15, seed=11)
    coins = backtest.load_coins(s)
    mm = MarketMode(LOOSE)
    t0 = min(c.t0 for c in coins)
    mm.add(t0 - 600, 100, 10)
    mm.add(t0 - 60, 90, 9)                                            # −10% just before: blocks for 1h
    desk = UpgradedPaperDesk(s, u=LOOSE, market=mm, rng=random.Random(0))
    traded = run_desk(desk, coins)
    assert all(t.entry_ts > t0 + 3500 for t in paper_mod.load_trades(desk.con))
    assert traded is not None


def test_upgraded_desk_charges_realistic_costs(tmp_path):
    s_a, s_b = paper_settings(tmp_path / "a"), paper_settings(tmp_path / "b")
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    for s in (s_a, s_b):
        synth_db(s.db_path, n=60, days=0.15, seed=11)
    coins = backtest.load_coins(s_a)
    free = run_desk(UpgradedPaperDesk(s_a, u=LOOSE, rng=random.Random(0)), coins)
    costly_u = UpgradeSettings(**{**LOOSE.__dict__, "network_fee_usd": 0.02, "mev_buy_pct": 0.005})
    costly = run_desk(UpgradedPaperDesk(s_b, u=costly_u, rng=random.Random(0)), coins)
    assert set(free) == set(costly)
    assert all(costly[m] < free[m] for m in free)


# ---------- before/after report ----------
def test_compare_report(tmp_path):
    s = with_overrides(Settings(), db_path=str(tmp_path / "c.db"))
    synth_db(s.db_path, n=300, days=6, seed=3)
    coins = backtest.load_coins(s)
    same = compare.compare(coins, {}, s, LOOSE, None)
    assert [t.mint for t in same["before"][0]] == [t.mint for t in same["after"][0]]
    r = compare.compare(coins, {}, s, U, None)                        # default: pool age 3 min blocks some
    lines = compare.report(r, s, len(coins), 6, "off")
    assert len(lines) <= 20 and lines[0].startswith("BEFORE vs AFTER")


def test_extra_checks_order():
    mm = MarketMode(U)
    mm.add(0, 100, 10)
    mm.add(60, 50, 5)
    assert extra_checks(bad(mintAuthority="X" * 44), 600, 60, mm, U).startswith("audit: authorities")
    assert extra_checks(CLEAN, 600, 60, mm, U).startswith("market:")
    assert extra_checks(CLEAN, 600, 60, None, U) is None
