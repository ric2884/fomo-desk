"""Short analysis summary — never more than 20 lines. Paste this into chat, not raw data or logs.

Run:  py -m desk.summary            backtest: tune on first 70% of days, report the unseen 30%
      py -m desk.summary --paper    paper-trading results so far
"""
from __future__ import annotations

import argparse
import time

from . import backtest
from .config import Settings, load_settings
from .ledger import Ledger
from .strategy import Trade

MAX_LINES = 20


def summarize(title: str, trades: list[Trade], s: Settings, verdict: str | None = None) -> list[str]:
    led = Ledger(s)
    for t in sorted(trades, key=lambda t: t.exit_ts):
        led.on_open(t.mint, t.entry_ts, t.cost_usd)
        led.on_close(t.mint, t.exit_ts, t.proceeds_usd)
    m = backtest.metrics({"trades": trades, "equity": led.equity_curve}, s)
    n = m["trades"]
    avg = m["net"] / n if n else 0.0
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    out = [title,
           f"Trades {n} · win rate {m['win_rate']:.0f}% · avg P/L ${avg:+.2f} · net ${m['net']:+.2f}",
           f"Max drawdown {m['max_dd_pct']:.1f}% · profit factor {pf} · net without 3 best ${m['net_ex_top3']:+.2f}"]
    if verdict:
        out.append(f"Verdict: {verdict}")
    ranked = sorted(trades, key=lambda t: t.pnl, reverse=True)
    fmt = lambda t: f"  {t.symbol[:10]:<10} ${t.pnl:+6.2f} ({t.pnl_pct:+4.0f}%) {t.exit_reason}"  # noqa: E731
    if ranked:
        out.append("5 best:")
        out += [fmt(t) for t in ranked[:5]]
        out.append("5 worst:")
        out += [fmt(t) for t in ranked[::-1][:5]]
    return out[:MAX_LINES]


def backtest_summary(s: Settings) -> list[str]:
    coins = backtest.load_coins(s)
    if not coins:
        return ["No coins recorded yet."]
    train, test, days = backtest.split_by_days(coins, s.train_frac)
    chosen, tried = backtest.tune(train, s)
    res = backtest.run(test, chosen)
    m = backtest.metrics(res, s)
    title = (f"BACKTEST · {len(coins)} coins, {len(days)} days · tested on last {len(test)} coins (unseen) · "
             f"tuned dip {chosen.dip_min} stop {chosen.stop} trail {chosen.trail}")
    return summarize(title, res["trades"], s, backtest.verdict(m))


def paper_summary(s: Settings) -> list[str]:
    from .paper import PaperDesk, load_trades
    desk = PaperDesk(s)
    trades = load_trades(desk.con)
    return summarize(f"PAPER · balance ${desk.ledger.equity:.2f} · open {len(desk.positions)}", trades, s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", action="store_true")
    a = ap.parse_args()
    s = load_settings()
    t0 = time.time()
    lines = paper_summary(s) if a.paper else backtest_summary(s)
    print("\n".join(lines))
    if not a.paper and len(lines) < MAX_LINES:
        print(f"(took {time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
