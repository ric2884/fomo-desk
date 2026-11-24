"""Before vs after: replay all recorded coins through the OLD rules and the OLD rules + upgrade.
Same data, same money limits, same random buy-failures per coin. At most 20 lines.

Run:  py -m desk.compare                      (uses this folder's data\\desk.db)
      py -m desk.compare --db C:\\path\\desk.db (read another copy, e.g. the running desk's)
      py -m desk.compare --no-market          (skip fetching BTC/SOL history)

Paper trading = this replay: a test proves paper makes exactly the backtest's trades on the same data.
"""
from __future__ import annotations

import argparse
import json
import random
import zlib
from collections import Counter

from . import audit, backtest, db, realcosts
from .config import Settings, load_settings, with_overrides
from .ledger import Ledger
from .market import MarketMode, load_history
from .strategy import Trade, check_filters, find_entry, simulate
from .upgrade import extra_checks
from .upgrade_settings import UpgradeSettings, load_upgrade_settings


def _label(why: str) -> str:
    if why.startswith("audit: "):
        return next((n for n in audit.LAYERS if why[7:].startswith(n)), why[7:])
    return "market mode"


def coin_rng(mint: str, seed: int) -> random.Random:
    return random.Random(zlib.crc32(mint.encode()) ^ seed)


def apply_limits(trades: list[Trade], s: Settings) -> tuple[list[Trade], Ledger]:
    """Money limits in time order — same logic as backtest.run."""
    led, open_, taken = Ledger(s), [], []
    for t in sorted(trades, key=lambda t: t.entry_ts):
        for o in sorted([o for o in open_ if o.exit_ts <= t.entry_ts], key=lambda o: o.exit_ts):
            open_.remove(o)
            led.on_close(o.mint, o.exit_ts, o.proceeds_usd)
        if led.block_reason(t.entry_ts, t.cost_usd):
            continue
        led.on_open(t.mint, t.entry_ts, t.cost_usd)
        open_.append(t)
        taken.append(t)
    for o in sorted(open_, key=lambda o: o.exit_ts):
        led.on_close(o.mint, o.exit_ts, o.proceeds_usd)
    return taken, led


def compare(coins, raws: dict, s: Settings, u: UpgradeSettings, market: MarketMode | None) -> dict:
    before, after, blocked_by, blocked_trades, extra = [], [], Counter(), [], 0.0
    for c in coins:
        t_old, _ = simulate(c.mint, c.symbol, c.t0, c.snaps, c.rug, s, coin_rng(c.mint, s.seed))
        if t_old:
            before.append(t_old)
        snaps = [x for x in c.snaps if x.price is not None and x.liq is not None]
        i = find_entry(snaps, c.t0, s) if len(snaps) >= 3 else None
        if i is not None and check_filters(c.rug, snaps[i], s) is None:
            why = extra_checks(raws.get(c.mint), snaps[i].ts - c.t0, snaps[i].ts, market, u)
            if why:
                blocked_by[_label(why)] += 1
                if t_old:
                    blocked_trades.append(t_old)
                continue
        t_new, _ = simulate(c.mint, c.symbol, c.t0, c.snaps, c.rug, s, coin_rng(c.mint, s.seed))
        if t_new:
            extra += realcosts.apply(t_new, snaps, u)
            after.append(t_new)
    b, led_b = apply_limits(before, s)
    a, led_a = apply_limits(after, s)
    return {"before": (b, led_b), "after": (a, led_a), "blocked_by": blocked_by,
            "blocked_trades": blocked_trades, "extra": extra, "n_after_raw": len(after)}


def report(r: dict, s: Settings, n_coins: int, n_days: int, market_note: str) -> list[str]:
    mb = backtest.metrics({"trades": r["before"][0], "equity": r["before"][1].equity_curve}, s)
    ma = backtest.metrics({"trades": r["after"][0], "equity": r["after"][1].equity_curve}, s)
    pf = lambda m: "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"  # noqa: E731
    avg = lambda m: m["net"] / m["trades"] if m["trades"] else 0.0  # noqa: E731
    rows = [("trades", f"{mb['trades']}", f"{ma['trades']}"),
            ("win rate", f"{mb['win_rate']:.0f}%", f"{ma['win_rate']:.0f}%"),
            ("avg P/L per trade", f"${avg(mb):+.2f}", f"${avg(ma):+.2f}"),
            ("net after costs", f"${mb['net']:+.2f}", f"${ma['net']:+.2f}"),
            ("max drawdown", f"{mb['max_dd_pct']:.1f}%", f"{ma['max_dd_pct']:.1f}%"),
            ("profit factor", pf(mb), pf(ma)),
            ("net without 3 best", f"${mb['net_ex_top3']:+.2f}", f"${ma['net_ex_top3']:+.2f}")]
    bt = r["blocked_trades"]
    out = [f"BEFORE vs AFTER · {n_coins} coins, {n_days} days · same data, same $100 limits",
           f"{'':<20}{'before':>10}{'after':>10}"]
    out += [f"{k:<20}{v1:>10}{v2:>10}" for k, v1, v2 in rows]
    out.append("Blocked by new filters: " + (", ".join(f"{k} {v}" for k, v in r["blocked_by"].most_common(4)) or "none"))
    out.append(f"  old rules would have bought {len(bt)} of them (before money limits): "
               f"net ${sum(t.pnl for t in bt):+.2f}, {sum(1 for t in bt if t.pnl > 0)} winners")
    n = max(1, r["n_after_raw"])
    out.append(f"Extra realistic costs: ${r['extra']:.2f} total, ${r['extra'] / n:.2f} per trade")
    out.append(f"Market mode: {market_note}")
    delta = ma["net"] - mb["net"]
    out.append(f"Change in net: ${delta:+.2f} → " + ("upgrade helps on this data" if delta > 0 else
               "upgrade does not help on this data") + (" (few trades — weak evidence)" if mb["trades"] < 30 else ""))
    return out[:20]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--no-market", action="store_true")
    a = ap.parse_args()
    s = load_settings()
    if a.db:
        s = with_overrides(s, db_path=a.db)
    u = load_upgrade_settings()
    coins = backtest.load_coins(s)
    if not coins:
        print("No coins recorded in", s.db_path)
        return
    con = db.connect(s.db_path)
    raws = {}
    for row in con.execute("SELECT mint, raw_json FROM rugcheck ORDER BY ts"):
        raws.setdefault(row["mint"], json.loads(row["raw_json"]))
    days = int((coins[-1].t0 - coins[0].t0) / 86400) + 1
    market, note = None, "off (--no-market)"
    if not a.no_market:
        mm = MarketMode(u)
        if load_history(mm, days + 1):
            market, note = mm, "on — 1h rule only (hourly history; 15-min rule needs live data)"
        else:
            note = "NOT evaluated — could not fetch BTC/SOL history"
    print("\n".join(report(compare(coins, raws, s, u, market), s, len(coins), days, note)))


if __name__ == "__main__":
    main()
