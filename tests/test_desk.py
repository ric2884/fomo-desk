import json
import random

import pytest

from desk import backtest, db, fields
from desk.config import Settings, with_overrides
from desk.costs import fomo_fee, round_trip_pct, slippage
from desk.recorder import Recorder
from desk.strategy import Snap, check_filters, simulate

GOOD_RUG = {"mint_revoked": True, "freeze_revoked": True, "lp_locked_pct": 100.0, "top10_pct": 20.0,
            "creator": "C", "creator_pct": 1.0, "danger_risks": [], "rugged": False}


def S(**kw):
    return with_overrides(Settings(), fail_rate=0.0, **kw)


def path(prices, liq=50_000, step=15, t0=0.0):
    return [Snap(t0 + i * step, p, liq if not isinstance(liq, list) else liq[i]) for i, p in enumerate(prices)]


# ---------- costs ----------
def test_fee_table():
    assert fomo_fee(2) == 0.10
    assert fomo_fee(5) == pytest.approx(0.10)
    assert fomo_fee(10) == pytest.approx(0.20)
    assert fomo_fee(100) == 0.95
    assert fomo_fee(1000) == pytest.approx(5.0)


def test_slippage_floor_and_impact():
    s = Settings()
    assert slippage(5, 100_000, "buy", 600, s) == s.slip_min
    assert slippage(5, 100_000, "buy", 60, s) == s.slip_early_floor
    assert slippage(1000, 20_000, "sell", 600, s) == pytest.approx(0.10)


def test_round_trip_is_costly():
    rt = round_trip_pct(5, 40_000, 180, Settings())
    assert 7 < rt < 10   # ~4% fees + ~4.5% slippage


# ---------- filters ----------
def test_filters():
    s = Settings()
    snap = Snap(0, 1.0, 50_000)
    assert check_filters(GOOD_RUG, snap, s) is None
    assert check_filters(None, snap, s) == "no rugcheck report"
    assert check_filters({**GOOD_RUG, "mint_revoked": False}, snap, s) == "mint not revoked"
    assert check_filters({**GOOD_RUG, "top10_pct": 45}, snap, s).startswith("top10")
    assert check_filters(GOOD_RUG, Snap(0, 1.0, 10_000), s).startswith("liquidity")


# ---------- strategy mechanics ----------
def run1(prices, liq=50_000, **kw):
    return simulate("M", "X", 0.0, path(prices, liq), GOOD_RUG, S(**kw), random.Random(1))


def test_no_entry_without_dip():
    t, why = run1([1.0 + i * 0.01 for i in range(100)])
    assert t is None and why == "no dip entry"


def test_stop_loss():
    # rise to 2.0, dip 20% to 1.6 at ~2.5 min, then collapse
    prices = [1.0] * 4 + [2.0] * 6 + [1.6] * 3 + [1.2] * 10
    t, _ = run1(prices)
    assert t.exit_reason == "stop"
    assert -35 < t.pnl_pct < -20      # 25% move + costs


def test_take_profit_then_trail():
    prices = [2.0] * 9 + [1.6] * 3 + [2.5] * 3 + [4.0] * 3 + [2.6] * 5
    t, _ = run1(prices)
    assert [l[1] for l in t.legs] == ["take profit", "trail"]
    assert t.pnl > 0


def test_rug_is_total_loss_of_remaining():
    prices = [2.0] * 9 + [1.6] * 3 + [1.6] * 3 + [0.0] * 3
    liq = [50_000] * 15 + [0] * 3
    t, _ = run1(prices, liq)
    assert t.exit_reason == "rug"
    assert t.proceeds_usd == 0 and t.pnl == -5.0


def test_time_stop():
    prices = [2.0] * 9 + [1.65] * 400
    t, _ = run1(prices)
    assert t.exit_reason == "time"
    assert t.pnl < 0   # flat price still loses to costs


# ---------- money limits ----------
def coin(i, prices, liq=50_000, t0=0.0):
    return backtest.Coin(f"m{i}", f"C{i}", t0, path(prices, liq, t0=t0), GOOD_RUG)


def test_kill_switch_and_daily_limit():
    loser = [2.0] * 9 + [1.6] * 3 + [0.0] * 3
    liq = [50_000] * 12 + [0] * 3
    coins = [coin(i, loser, liq, t0=i * 600.0) for i in range(40)]
    res = backtest.run(coins, S())
    # $5 total loss each: daily limit $8 → 2 trades on day 1, then blocked
    assert len(res["trades"]) == 2
    assert res["blocked"]["daily loss limit"] > 0


def test_kill_switch_over_days():
    loser = [2.0] * 9 + [1.6] * 3 + [0.0] * 3
    liq = [50_000] * 12 + [0] * 3
    coins = [coin(i, loser, liq, t0=i * 86400.0) for i in range(30)]
    res = backtest.run(coins, S(daily_loss_limit=100, weekly_loss_limit=1000))
    assert res["final"] == pytest.approx(60.0)
    assert res["killed"]


# ---------- recorder parsing + storage ----------
def test_parse_migration():
    assert fields.parse_migration({"message": "Successfully subscribed"}) is None
    m = fields.parse_migration({"signature": "sig", "mint": "MINT", "txType": "migrate", "pool": "pump-amm"})
    assert m == {"mint": "MINT", "pool": "pump-amm", "signature": "sig"}


DEX_SAMPLE = [
    {"chainId": "solana", "dexId": "raydium", "baseToken": {"address": "MINT", "name": "Coin", "symbol": "CN"},
     "priceUsd": "0.0001", "liquidity": {"usd": 9000}, "txns": {"m5": {"buys": 1, "sells": 1}}},
    {"chainId": "solana", "dexId": "pumpswap", "baseToken": {"address": "MINT", "name": "Coin", "symbol": "CN"},
     "priceUsd": "0.00012", "liquidity": {"usd": 41000}, "marketCap": 120000,
     "volume": {"m5": 5000}, "txns": {"m5": {"buys": 40, "sells": 22}}},
]


def test_pick_pair_prefers_pumpswap():
    p = fields.parse_pair(fields.pick_pair(DEX_SAMPLE, "MINT"))
    assert p["price"] == 0.00012 and p["liq"] == 41000 and p["buys_m5"] == 40


def test_rugcheck_parse_excludes_pool():
    rep = {"mintAuthority": None, "freezeAuthority": None, "creator": "DEV",
           "markets": [{"pubkey": "P" * 44, "liquidityBAccount": "V" * 43, "lp": {"lpLockedPct": 100}}],
           "topHolders": [{"address": "V" * 43, "owner": "P" * 44, "pct": 60},
                          {"address": "a1", "owner": "DEV", "pct": 3},
                          {"address": "a2", "owner": "w2", "pct": 7}],
           "risks": [{"name": "Low liquidity", "level": "warn"}]}
    r = fields.parse_rugcheck(rep)
    assert r["top10_pct"] == 10 and r["creator_pct"] == 3 and r["lp_locked_pct"] == 100
    assert r["mint_revoked"] and r["danger_risks"] == []


def test_recorder_stores(tmp_path):
    s = with_overrides(Settings(), db_path=str(tmp_path / "t.db"))
    rec = Recorder(s)
    rec.on_migration({"signature": "sig", "mint": "MINT", "txType": "migrate", "pool": "pump-amm"})
    assert "MINT" in rec.due_mints(rec.active["MINT"] + 1)
    rec.store_snapshots(["MINT"], DEX_SAMPLE, rec.active["MINT"] + 15)
    row = rec.con.execute("SELECT * FROM snapshots").fetchone()
    assert row["liq"] == 41000
    assert rec.con.execute("SELECT symbol FROM coins").fetchone()[0] == "CN"
    assert rec.due_mints(rec.active["MINT"] + 5 * 3600) == [] and not rec.active


# ---------- full backtest on synthetic market ----------
def synth_db(path_db, n=300, days=10, seed=3):
    rnd = random.Random(seed)
    con = db.connect(path_db)
    for i in range(n):
        t0 = 1_700_000_000 + (i / n) * days * 86400
        mint = f"mint{i}"
        con.execute("INSERT INTO coins VALUES (?,?,?,?,?,?)", (mint, "n", f"S{i}", "pump-amm", t0, ""))
        p, liq, rugged = 1e-4, rnd.uniform(15_000, 80_000), False
        for k in range(240):
            ts = t0 + k * 15
            if not rugged and rnd.random() < 0.004:
                rugged = True
            if rugged:
                p, liq = 0.0, 0.0
            else:
                p *= max(0.05, 1 + rnd.gauss(-0.002, 0.06))
            con.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?)", (mint, ts, p, liq, 0, 0, 0, 0))
        rep = {"mintAuthority": None, "freezeAuthority": None, "creator": "D",
               "markets": [{"pubkey": "P", "lp": {"lpLockedPct": 100}}],
               "topHolders": [{"address": f"h{j}", "owner": f"o{j}", "pct": rnd.uniform(1, 5)} for j in range(10)],
               "risks": []}
        con.execute("INSERT INTO rugcheck VALUES (?,?,?)", (mint, t0 + 90, json.dumps(rep)))
    con.commit()


def test_backtest_end_to_end(tmp_path):
    s = with_overrides(Settings(), db_path=str(tmp_path / "s.db"))
    synth_db(s.db_path)
    coins = backtest.load_coins(s)
    train, test, days = backtest.split_by_days(coins, s.train_frac)
    assert len(days) >= 10 and train and test
    assert max(c.t0 for c in train) < min(c.t0 for c in test)   # no leakage
    chosen, tried = backtest.tune(train, s)
    assert len(tried) == 27
    m = backtest.metrics(backtest.run(test, chosen), s)
    assert m["trades"] > 0
    # a random-walk market has no edge: costs must make it lose
    assert m["net"] < 0
    assert backtest.verdict(m).startswith(("NO EDGE", "NOT ENOUGH"))


def test_rugcheck_parse_nested_market_objects():
    pool = "P" * 44
    vault = "V" * 44
    rep = {"mintAuthority": None, "freezeAuthority": None, "creator": "DEV",
           "markets": [{"pubkey": pool, "liquidityA": {"address": vault, "amount": 5},
                        "liquidityB": {"address": "W" * 44}, "lp": {"lpLockedPct": 100}}],
           "topHolders": [{"address": vault, "owner": pool, "pct": 60},
                          {"address": "a1", "owner": "DEV", "pct": 4},
                          {"address": "a2", "owner": "w2", "pct": 9}],
           "knownAccounts": {pool: {"name": "PumpSwap", "type": "AMM"}},
           "risks": [{"name": "Mutable metadata", "level": "danger"}, "junk"]}
    r = fields.parse_rugcheck(rep)
    assert r["top10_pct"] == 13 and r["creator_pct"] == 4
    assert r["danger_risks"] == ["Mutable metadata"]
