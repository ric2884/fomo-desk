"""Memecoin re-tune with walk-forward testing. Honest by construction:
settings for each test day are chosen using ONLY the days before it.

Run:  py -m desk.tune            report (at most 20 lines), changes nothing
      py -m desk.tune --apply    only if the report says PROFITABLE: writes settings.json for the live paper bot
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
import zlib
from collections import Counter, defaultdict

from . import backtest
from .config import ROOT, Settings, load_settings, with_overrides
from .strategy import Trade, simulate

MIN_TRAIN_DAYS = 3
MIN_TRAIN_TRADES = 10
MIN_TEST_TRADES = 30


def grid() -> list[dict]:
    """Entry type × signal strength × entry age × take-profit × stop. 108 settings."""
    out = []
    ages = [120, 900, 1800]                                  # later entry
    tps = {0.5: 45 * 60, 1.0: 90 * 60, 2.0: 180 * 60}       # bigger take-profit gets more time
    for kind, strengths in (("dip", [0.10, 0.20, 0.30]), ("momentum", [0.15, 0.30, 0.60])):
        for x, age, (tp, tstop), stop in itertools.product(strengths, ages, tps.items(), [0.15, 0.30]):
            o = {"entry_type": kind, "min_age_s": age, "max_entry_age_s": age + 1800,
                 "tp": tp, "time_stop_s": tstop, "stop": stop}
            if kind == "dip":
                o.update(dip_min=x, dip_max=x + 0.20)
            else:
                o.update(mom_up=x)
            out.append(o)
    return out


def label(o: dict | None) -> str:
    if o is None:
        return "sit out"
    sig = f"dip {o['dip_min']:.0%}" if o["entry_type"] == "dip" else f"mom +{o['mom_up']:.0%}"
    return f"{sig} age {o['min_age_s'] // 60}m tp +{o['tp']:.0%} stop {o['stop']:.0%}"


def coin_rng(mint: str, seed: int) -> random.Random:
    return random.Random(zlib.crc32(mint.encode()) ^ seed)


def candidates(coins, s: Settings) -> list[Trade]:
    out = []
    for c in coins:
        t, _ = simulate(c.mint, c.symbol, c.t0, c.snaps, c.rug, s, coin_rng(c.mint, s.seed))
        if t:
            out.append(t)
    return out


def limited(trades: list[Trade], s: Settings) -> dict:
    res = backtest.apply_limits(trades, s)
    res["m"] = backtest.metrics(res, s)
    return res


def walk_forward(coins, s: Settings, combos: list[dict], progress=None) -> dict:
    days = sorted({backtest.day_of(c.t0) for c in coins})
    per_combo = []
    for k, o in enumerate(combos):
        per_combo.append(candidates(coins, with_overrides(s, **o)))
        if progress:
            progress(k + 1, len(combos))
    by_day = [defaultdict(list) for _ in combos]
    for i, trades in enumerate(per_combo):
        for t in trades:
            by_day[i][backtest.day_of(t.entry_ts)].append(t)
    current = defaultdict(list)
    for t in candidates(coins, s):
        current[backtest.day_of(t.entry_ts)].append(t)

    oos, oos_current, chosen = [], [], []
    for k in range(min(MIN_TRAIN_DAYS, len(days) - 1), len(days)):
        train, test = days[:k], days[k]
        best, best_net = None, 0.0
        for i, o in enumerate(combos):
            r = limited([t for d in train for t in by_day[i][d]], with_overrides(s, **o))
            if r["m"]["trades"] >= MIN_TRAIN_TRADES and r["m"]["net"] > best_net:
                best, best_net = i, r["m"]["net"]
        chosen.append((test, None if best is None else combos[best]))
        if best is not None:
            oos += by_day[best][test]
        oos_current += current[test]
    test_days = [d for d, _ in chosen]
    full = [limited(per_combo[i], with_overrides(s, **o)) for i, o in enumerate(combos)]
    best_all = max(range(len(combos)), key=lambda i: full[i]["m"]["net"]) if combos else None
    return {"days": days, "test_days": test_days, "chosen": chosen,
            "oos": limited(oos, s), "oos_current": limited(oos_current, s),
            "best_all": None if best_all is None else combos[best_all],
            "best_all_m": None if best_all is None else full[best_all]["m"]}


def daily(res: dict, s: Settings, days: list[str]) -> tuple[int, float]:
    pnl = Counter()
    for t in res["trades"]:
        pnl[backtest.day_of(t.exit_ts)] += t.pnl
    hit = sum(1 for d in days if pnl[d] >= s.day_lock_pct * s.bankroll_usd)
    return hit, (sum(pnl[d] for d in days) / len(days) if days else 0.0)


def passes(m: dict) -> bool:
    return m["trades"] >= MIN_TEST_TRADES and m["net"] > 0


def report(wf: dict, s: Settings, n_coins: int, btc: str | None = None) -> list[str]:
    m, mc = wf["oos"]["m"], wf["oos_current"]["m"]
    hit, avg = daily(wf["oos"], s, wf["test_days"])
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    used = Counter(label(o) for _, o in wf["chosen"])
    out = [f"MEMECOIN RE-TUNE · walk-forward · {n_coins} coins, {len(wf['days'])} days · {len(grid())} settings tried",
           f"Each of the last {len(wf['test_days'])} days tested with settings picked from earlier days only",
           f"UNSEEN DAYS: {m['trades']} trades · win {m['win_rate']:.0f}% · net ${m['net']:+.2f} · PF {pf}",
           f"  max drawdown {m['max_dd_pct']:.1f}% · net without 3 best ${m['net_ex_top3']:+.2f}",
           f"  days ≥ +1%: {hit} of {len(wf['test_days'])} · average day ${avg:+.2f} ({avg / s.bankroll_usd:+.2%})",
           f"Current settings, same days: {mc['trades']} trades · net ${mc['net']:+.2f}"]
    if btc:
        out.append(f"BTC buy-and-hold, same days: {btc}")
    out.append("Settings picked per test day: " + "; ".join(f"{k} ×{v}" for k, v in used.most_common(3)))
    if wf["best_all"]:
        mb = wf["best_all_m"]
        out.append(f"Best on ALL days (in-sample, optimistic): {label(wf['best_all'])} → net ${mb['net']:+.2f}, "
                   f"{mb['trades']} trades")
    if passes(m):
        out.append("VERDICT: PROFITABLE ON UNSEEN DAYS (≥ 30 trades). Apply with: py -m desk.tune --apply")
    else:
        why = f"only {m['trades']} unseen trades" if m["trades"] < MIN_TEST_TRADES else "loses money on unseen days"
        out.append(f"VERDICT: NO PROFITABLE SETTINGS FOUND ({why}). Keeping the current settings.")
    return out[:20]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    s = load_settings()
    coins = backtest.load_coins(s)
    if not coins:
        print("No coins recorded yet.")
        return
    prog = lambda k, n: print(f"\r  testing settings {k}/{n}…", end="", file=sys.stderr, flush=True)  # noqa: E731
    wf = walk_forward(coins, s, grid(), prog)
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
    btc = None
    try:
        from .majors.data import btc_change
        btc = btc_change(wf["test_days"])
    except Exception:  # noqa: BLE001 — majors data is optional
        pass
    lines = report(wf, s, len(coins), btc)
    print("\n".join(lines))
    if a.apply:
        if not passes(wf["oos"]["m"]):
            print("NOT applied: the settings did not pass on unseen days.")
            return
        path = ROOT / "settings.json"
        cur = json.loads(path.read_text()) if path.exists() else {}
        cur.update(wf["best_all"])
        path.write_text(json.dumps(cur, indent=1))
        print(f"Applied to {path}. Now reset paper and restart it: py -m desk.paper --reset, then py -m desk.paper")


if __name__ == "__main__":
    main()
