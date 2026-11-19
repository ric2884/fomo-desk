"""STEP 3 — replay recorded coins through the strategy, with costs and money limits.

Run:  python -m desk.backtest            (defaults only)
      python -m desk.backtest --tune     (tune 3 parameters on the first 70% of days, test on the rest)
Writes reports/report.md and reports/equity.svg
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import db, fields
from .config import ROOT, Settings, load_settings, with_overrides
from .strategy import Snap, Trade, simulate

GRID = {"dip_min": [0.10, 0.15, 0.20], "stop": [0.15, 0.20, 0.25], "trail": [0.20, 0.30, 0.40]}


@dataclass
class Coin:
    mint: str
    symbol: str
    t0: float
    snaps: list[Snap]
    rug: dict | None


def load_coins(s: Settings) -> list[Coin]:
    con = db.connect(s.db_path)
    snaps = defaultdict(list)
    for r in con.execute("SELECT mint, ts, price, liq FROM snapshots ORDER BY mint, ts"):
        snaps[r["mint"]].append(Snap(r["ts"], r["price"], r["liq"]))
    rugs = {}
    for r in con.execute("SELECT mint, raw_json FROM rugcheck ORDER BY ts"):
        rugs.setdefault(r["mint"], fields.parse_rugcheck(json.loads(r["raw_json"])))
    return [Coin(r["mint"], r["symbol"] or r["mint"][:6], r["migrated_at"], snaps[r["mint"]], rugs.get(r["mint"]))
            for r in con.execute("SELECT * FROM coins ORDER BY migrated_at")]


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def week_of(ts: float) -> str:
    y, w, _ = datetime.fromtimestamp(ts, timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def run(coins: list[Coin], s: Settings) -> dict:
    """Simulate every coin, then apply the money limits in time order (STEP 7)."""
    rng = random.Random(s.seed)
    cands: list[Trade] = []
    skips = Counter()
    for c in coins:
        t, why = simulate(c.mint, c.symbol, c.t0, c.snaps, c.rug, s, rng)
        if t:
            cands.append(t)
        else:
            skips[why.split(" ")[0] if why.startswith(("top10", "lp", "liquidity", "creator", "danger")) else why] += 1
    cands.sort(key=lambda t: t.entry_ts)

    cash = s.bankroll_usd
    open_: list[Trade] = []
    taken: list[Trade] = []
    day_pnl = defaultdict(float)
    week_pnl = defaultdict(float)
    day_count = Counter()
    blocked = Counter()
    killed = False
    equity = [(cands[0].entry_ts if cands else 0, cash)]

    def close_until(ts: float) -> None:
        nonlocal cash
        for t in sorted([t for t in open_ if t.exit_ts <= ts], key=lambda t: t.exit_ts):
            open_.remove(t)
            cash += t.proceeds_usd
            day_pnl[day_of(t.exit_ts)] += t.pnl
            week_pnl[week_of(t.exit_ts)] += t.pnl
            equity.append((t.exit_ts, cash + sum(o.cost_usd for o in open_)))

    for t in cands:
        close_until(t.entry_ts)
        d, w = day_of(t.entry_ts), week_of(t.entry_ts)
        if killed or cash + sum(o.cost_usd for o in open_) <= s.kill_switch_balance:
            killed = True
            blocked["kill switch"] += 1
            continue
        if day_pnl[d] <= -s.daily_loss_limit:
            blocked["daily loss limit"] += 1
            continue
        if week_pnl[w] <= -s.weekly_loss_limit:
            blocked["weekly loss limit"] += 1
            continue
        if len(open_) >= s.max_open:
            blocked["max open positions"] += 1
            continue
        if day_count[d] >= s.max_trades_day:
            blocked["max trades/day"] += 1
            continue
        exposure = sum(o.cost_usd for o in open_) + t.cost_usd
        if cash < t.cost_usd or exposure > s.max_exposure_frac * (cash + exposure - t.cost_usd):
            blocked["not enough cash / exposure"] += 1
            continue
        cash -= t.cost_usd
        open_.append(t)
        taken.append(t)
        day_count[d] += 1
    close_until(float("inf"))
    return {"trades": taken, "skips": skips, "blocked": blocked, "equity": equity,
            "final": cash, "killed": killed}


def metrics(res: dict, s: Settings) -> dict:
    tr = res["trades"]
    wins = [t.pnl for t in tr if t.pnl > 0]
    losses = [t.pnl for t in tr if t.pnl <= 0]
    net = sum(t.pnl for t in tr)
    top3 = sorted((t.pnl for t in tr), reverse=True)[:3]
    peak, mdd = s.bankroll_usd, 0.0
    for _, e in res["equity"]:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak)
    return {
        "trades": len(tr),
        "win_rate": len(wins) / len(tr) * 100 if tr else 0.0,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "net": net,
        "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else float("inf") if wins else 0.0,
        "max_dd_pct": mdd * 100,
        "net_ex_top3": net - sum(t for t in top3 if t > 0),
        "exit_reasons": Counter(t.exit_reason for t in tr),
    }


def split_by_days(coins: list[Coin], frac: float) -> tuple[list[Coin], list[Coin], list[str]]:
    days = sorted({day_of(c.t0) for c in coins})
    n_train = max(1, int(round(len(days) * frac))) if len(days) > 1 else len(days)
    train_days = set(days[:n_train])
    return ([c for c in coins if day_of(c.t0) in train_days],
            [c for c in coins if day_of(c.t0) not in train_days], days)


def tune(train: list[Coin], s: Settings) -> tuple[Settings, list[tuple[dict, dict]]]:
    tried = []
    for combo in itertools.product(*GRID.values()):
        kw = dict(zip(GRID, combo))
        m = metrics(run(train, with_overrides(s, **kw)), s)
        tried.append((kw, m))
    ok = [x for x in tried if x[1]["trades"] >= s.min_trades_for_tuning] or tried
    best = max(ok, key=lambda x: x[1]["net"])
    return with_overrides(s, **best[0]), tried


def verdict(m: dict) -> str:
    if m["trades"] < 30:
        return "NOT ENOUGH DATA — fewer than 30 test trades. Keep the recorder running and re-run."
    if m["net"] <= 0:
        return "NO EDGE FOUND — the strategy loses money after costs on unseen days."
    if m["net_ex_top3"] <= 0:
        return "NO EDGE FOUND — profit depends on 1–3 lucky trades."
    if m["profit_factor"] < 1.3:
        return "WEAK — positive, but profit factor below 1.3. Not enough margin for live costs."
    return "POSSIBLE EDGE — move to paper trading (STEP 4) and let the gate decide."


def fmt(m: dict) -> str:
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    return (f"| trades | {m['trades']} |\n| win rate | {m['win_rate']:.1f}% |\n"
            f"| avg win | ${m['avg_win']:.2f} |\n| avg loss | ${m['avg_loss']:.2f} |\n"
            f"| **net after costs** | **${m['net']:.2f}** |\n| profit factor | {pf} |\n"
            f"| max drawdown | {m['max_dd_pct']:.1f}% |\n| net excluding 3 best | ${m['net_ex_top3']:.2f} |\n"
            f"| exits | {dict(m['exit_reasons'])} |\n")


def equity_svg(curves: dict[str, list[tuple[float, float]]], path: Path) -> None:
    W, H, P = 800, 320, 40
    pts = [p for c in curves.values() for p in c]
    if len(pts) < 2:
        path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="60"><text x="10" y="30">no trades</text></svg>')
        return
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    y0, y1 = min(y0, 100) - 2, max(y1, 100) + 2
    sx = lambda x: P + (x - x0) / ((x1 - x0) or 1) * (W - 2 * P)  # noqa: E731
    sy = lambda y: H - P - (y - y0) / (y1 - y0) * (H - 2 * P)    # noqa: E731
    colors = ["#2a7de1", "#e1662a"]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="sans-serif" font-size="12">',
           f'<rect width="{W}" height="{H}" fill="#fff"/>',
           f'<line x1="{P}" x2="{W-P}" y1="{sy(100):.1f}" y2="{sy(100):.1f}" stroke="#aaa" stroke-dasharray="4"/>',
           f'<text x="4" y="{sy(100)+4:.1f}" fill="#666">$100</text>',
           f'<text x="4" y="{sy(y1)+4:.1f}" fill="#666">${y1:.0f}</text>',
           f'<text x="4" y="{sy(y0)+4:.1f}" fill="#666">${y0:.0f}</text>']
    for (name, c), col in zip(curves.items(), colors):
        if len(c) > 1:
            d = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in c)
            out.append(f'<polyline fill="none" stroke="{col}" stroke-width="2" points="{d}"/>')
        out.append(f'<text x="{W-P-120}" y="{20 + 16 * list(curves).index(name)}" fill="{col}">{name}</text>')
    out.append("</svg>")
    path.write_text("\n".join(out))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tune", action="store_true")
    a = ap.parse_args()
    s = load_settings()
    coins = load_coins(s)
    if not coins:
        print("No coins recorded yet. Run the recorder first: python -m desk.recorder")
        return
    train, test, days = split_by_days(coins, s.train_frac)
    rep = [f"# Backtest report\n", f"Data: {len(coins)} coins over {len(days)} days "
           f"({days[0]} → {days[-1]}). Train: {len(train)} coins, test: {len(test)} coins.\n"]

    chosen = s
    if a.tune:
        chosen, tried = tune(train, s)
        rep.append(f"Tuned on TRAIN only: tried {len(tried)} combinations of {list(GRID)}. "
                   f"Chosen: dip_min={chosen.dip_min}, stop={chosen.stop}, trail={chosen.trail}.\n")
    r_train, r_test = run(train, chosen), run(test, chosen)
    m_train, m_test = metrics(r_train, s), metrics(r_test, s)
    rep += ["\n## Train (used for tuning — optimistic)\n\n| | |\n|---|---|\n", fmt(m_train),
            "\n## Test (unseen days — this is the real number)\n\n| | |\n|---|---|\n", fmt(m_test),
            f"\n## Verdict\n\n**{verdict(m_test)}**\n",
            f"\n## Why coins were skipped (all days)\n\n{dict((r_train['skips'] + r_test['skips']).most_common())}\n",
            f"\nBlocked by money limits: {dict(r_train['blocked'] + r_test['blocked'])}\n"]
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    (out / "report.md").write_text("".join(rep))
    equity_svg({"train": r_train["equity"], "test": r_test["equity"]}, out / "equity.svg")
    print("".join(rep))
    print(f"\nSaved {out / 'report.md'} and {out / 'equity.svg'}")


if __name__ == "__main__":
    main()
