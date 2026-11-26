"""Short analysis summary — never more than 20 lines. Paste this into chat, not raw data or logs.

Run:  py -m desk.summary            backtest: tune on first 70% of days, report the unseen 30%
      py -m desk.summary --paper    paper-trading results so far
"""
from __future__ import annotations

import argparse
import time

from . import backtest
from .config import Settings, load_settings
from .ledger import Ledger, day_of
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
    out.append(daily_line(trades, s))
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


def daily_line(trades: list[Trade], s: Settings) -> str:
    """Days reaching +1%, average day, and BTC buy-and-hold over the same days."""
    from collections import Counter
    from datetime import datetime, timedelta, timezone
    if not trades:
        return "Days ≥ +1%: no trades"
    pnl = Counter()
    for t in trades:
        pnl[day_of(t.exit_ts)] += t.pnl
    first = datetime.strptime(day_of(min(t.entry_ts for t in trades)), "%Y-%m-%d")
    last = datetime.strptime(day_of(max(t.exit_ts for t in trades)), "%Y-%m-%d")
    days = [(first + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((last - first).days + 1)]
    hit = sum(1 for d in days if pnl[d] >= s.day_lock_pct * s.bankroll_usd)
    avg = sum(pnl[d] for d in days) / len(days)
    btc = None
    try:
        from .majors.data import btc_change
        btc = btc_change(days)
    except Exception:  # noqa: BLE001 — majors data optional
        pass
    return (f"Days ≥ +1%: {hit} of {len(days)} · avg day ${avg:+.2f} ({avg / s.bankroll_usd:+.2%})"
            + (f" · BTC hold same days {btc}" if btc else ""))


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
