"""Majors walk-forward test: each strategy vs buy-and-hold BTC, after costs, on UNSEEN periods.

How it stays honest:
- Every 6 months from 2023-01, each strategy's settings are re-picked using ONLY earlier data,
  then traded on the next 6 months it has never seen. Only those unseen months are reported.
- Real money limits on ($100, daily/weekly loss limits, kill switch, daily profit rule).

Run:  py -m desk.majors.backtest      (needs py -m desk.majors.data first; ~5–10 min)
Writes data/majors_choice.json with the best strategy for live paper trading.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings, load_settings
from . import data
from .config import MajorsSettings
from .engine import Engine
from .strategies import FAMILIES, combos, make

DAY = 86400


ATR_KS = (0.0, 3.0, 6.0)          # stop-loss: off, 3 ATRs, 6 ATRs


def run(ts_list, bars, ms, s, funding, schedule, start_ts, end_ts, limits=True) -> Engine:
    """schedule: list of (from_ts, family, params, allow_short, atr_k). Strategies are rebuilt and warmed up
    on the bars before each switch; positions carry over."""
    eng = Engine(ms, s, {}, funding, limits=limits)
    sched = sorted(schedule)
    k = -1
    idx_start = next((i for i, t in enumerate(ts_list) if t >= start_ts), len(ts_list))
    for i in range(idx_start, len(ts_list)):
        ts = ts_list[i]
        if ts > end_ts:
            break
        while k + 1 < len(sched) and sched[k + 1][0] <= ts:
            k += 1
            _, fam, params, sh, atr_k = sched[k]
            eng.strats = {sym: make(fam, params, sh) for sym in ms.symbols}
            eng.atr_k = atr_k
            for j in range(max(0, i - ms.warm_bars), i):                   # warm-up: indicators only
                for sym, b in bars[ts_list[j]].items():
                    if sym in eng.strats:
                        eng.strats[sym].update(b, 0, 0, ts_list[j])
                        if k == 0:
                            eng.observe(sym, b)
        eng.on_bar(ts, bars[ts])
    eng.finish()
    return eng


def daily_returns(eng: Engine) -> list[tuple[str, float]]:
    return [(d, (end / start - 1) if start > 0 else 0.0) for d, start, end, _ in eng.days]


def sharpe(rets: list[float]) -> float:
    if len(rets) < 20:
        return -9.0
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
    return m / sd * math.sqrt(365) if sd > 0 else -9.0


def folds(ms: MajorsSettings, last_ts: int) -> list[int]:
    out, d = [], datetime.strptime(ms.oos_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    while d.timestamp() < last_ts:
        out.append(int(d.timestamp()))
        m = d.month - 1 + ms.fold_months
        d = d.replace(year=d.year + m // 12, month=m % 12 + 1)
    return out


def stats(eng: Engine, s: Settings, start_ts: int, end_ts: int) -> dict:
    total = eng.total
    years = max((end_ts - start_ts) / (365 * DAY), 1e-9)
    peak, mdd = s.bankroll_usd, 0.0
    for _, _, end, _ in eng.days:
        peak = max(peak, end)
        mdd = max(mdd, (peak - end) / peak if peak else 0)
    n = len(eng.days) or 1
    hit = sum(1 for d in eng.days if d[3])
    avg = sum((e / st - 1) for _, st, e, _ in eng.days if st > 0) / n
    cagr = (total / s.bankroll_usd) ** (1 / years) - 1 if total > 0 else -1.0
    return {"end": total, "cagr": cagr, "mdd": mdd, "hit": hit, "days": n, "avg": avg,
            "banked": eng.banked, "killed": eng.killed, "fees": eng.fees, "funding": eng.funding_paid,
            "trades": eng.trades, "sharpe": sharpe([r for _, r in daily_returns(eng)])}


def btc_hold(ts_list, bars, s: Settings, ms: MajorsSettings, start_ts, end_ts) -> dict:
    """Buy BTC at the first open, hold, no limits. One taker fee + slippage in and out."""
    seg = [t for t in ts_list if start_ts <= t <= end_ts and "BTCUSDT" in bars[t]]
    p0 = bars[seg[0]]["BTCUSDT"][0] * (1 + ms.slippage("BTCUSDT"))
    qty = s.bankroll_usd * (1 - ms.fee) / p0
    days, day, start, peak, mdd = [], None, s.bankroll_usd, s.bankroll_usd, 0.0
    val = s.bankroll_usd
    for t in seg:
        d = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        if d != day:
            if day:
                days.append((start, val, hi >= start * (1 + s.day_lock_pct)))
            day, start, hi = d, val, val
        val = qty * bars[t]["BTCUSDT"][3]
        hi = max(hi, val)                         # hourly closes, same as the bots are measured
        peak = max(peak, val)
        mdd = max(mdd, (peak - val) / peak)
    days.append((start, val, hi >= start * (1 + s.day_lock_pct)))
    end = val * (1 - ms.fee - ms.slippage("BTCUSDT"))
    years = max((end_ts - start_ts) / (365 * DAY), 1e-9)
    return {"end": end, "cagr": (end / s.bankroll_usd) ** (1 / years) - 1, "mdd": mdd,
            "hit": sum(1 for d in days if d[2]), "days": len(days),
            "avg": sum(e / st - 1 for st, e, _ in days) / len(days), "banked": 0.0, "killed": False}


def walk_forward(ts_list, bars, funding, ms: MajorsSettings, s: Settings, progress=None) -> dict:
    first, last = ts_list[0], ts_list[-1]
    starts = folds(ms, last)
    oos_start = starts[0]
    # 1) every setting once over the whole history (no money limits) → daily returns to choose from
    cs = [(f, p, sh, k) for (f, p, sh) in combos() for k in ATR_KS]
    rets = {}
    for k, (fam, params, sh, atr_k) in enumerate(cs):
        eng = run(ts_list, bars, ms, s, funding, [(first, fam, params, sh, atr_k)], first, last, limits=False)
        rets[(fam, params, sh, atr_k)] = daily_returns(eng)
        if progress:
            progress(k + 1, len(cs) + len(FAMILIES))
    # 2) per family, per fold: pick the setting with the best Sharpe on data BEFORE the fold
    results, schedules = {}, {}
    for n, fam in enumerate(FAMILIES):
        sched = []
        for f in starts:
            cut = datetime.fromtimestamp(f, timezone.utc).strftime("%Y-%m-%d")
            best = max(((p, sh, k) for (fa, p, sh, k) in rets if fa == fam),
                       key=lambda ps: sharpe([r for d, r in rets[(fam, *ps)] if d < cut]))
            sched.append((f, fam, *best))
        eng = run(ts_list, bars, ms, s, funding, sched, oos_start, last, limits=True)
        results[fam] = stats(eng, s, oos_start, last)
        schedules[fam] = sched
        if progress:
            progress(len(cs) + n + 1, len(cs) + len(FAMILIES))
    return {"results": results, "schedules": schedules, "btc": btc_hold(ts_list, bars, s, ms, oos_start, last),
            "bank": s.bank_mode,
            "oos_start": oos_start, "last": last, "first": first}


def report(wf: dict, ms: MajorsSettings, funding_real: bool) -> list[str]:
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m")  # noqa: E731
    out = [f"MAJORS · {' '.join(x[:-4] for x in ms.symbols)} · 1h bars {fmt(wf['first'])}→{fmt(wf['last'])} · "
           f"{ms.leverage:g}× · fee {ms.fee:.2%}/side + slippage + funding ({'real' if funding_real else 'estimated'})",
           f"UNSEEN {fmt(wf['oos_start'])}→{fmt(wf['last'])}: settings + stop-loss re-picked every {ms.fold_months} "
           f"months from earlier data only · $100 limits · {wf.get('bank', 'weekly')} profit bank",
           f"{'strategy':<16}{'per year':>9}{'max DD':>8}{'days≥+1%':>10}{'avg day':>9}{'banked':>8}{'end $':>8}"]
    rows = list(wf["results"].items()) + [("BTC buy & hold", wf["btc"])]
    for name, r in rows:
        tag = " KILLED" if r.get("killed") else ""
        out.append(f"{name[:15]:<16}{r['cagr']:>+9.1%}{r['mdd']:>8.1%}{r['hit']:>5}/{r['days']:<4}"
                   f"{r['avg']:>+9.2%}{r['banked']:>8.2f}{r['end']:>8.2f}{tag}")
    best = max(wf["results"], key=lambda f: wf["results"][f]["sharpe"])
    b, h = wf["results"][best], wf["btc"]
    last_pick = wf["schedules"][best][-1]
    stop = f"stop {last_pick[4]:g}×ATR" if last_pick[4] else "no stop-loss"
    out.append(f"Best (risk-adjusted): {best} — latest {last_pick[2]}, "
               f"{'long+short' if last_pick[3] else 'long only'}, {stop} → paper trading this one live")
    out.append(f"vs BTC buy-and-hold: {b['cagr'] - h['cagr']:+.1%} per year, drawdown {b['mdd']:.0%} vs {h['mdd']:.0%}")
    out.append(f"Costs for best: fees ${b['fees']:.2f}, funding ${b['funding']:+.2f}, {b['trades']} positions opened")
    hit_rate = b["hit"] / max(1, b["days"])
    out.append(f"+1%/day target: reached on {hit_rate:.0%} of days · average day {b['avg']:+.2%} "
               f"(1%/day would be {(1.01 ** 365 - 1):.0%}/yr)")
    beat = b["cagr"] > h["cagr"]
    out.append("VERDICT: " + ("best strategy BEAT buy-and-hold on unseen data — now prove it live on paper (14 days)."
                              if beat else "no strategy beat simply holding BTC on unseen data. Paper-trading the "
                              "best anyway to learn, with zero money at risk."))
    return out[:20]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leverage", type=float, default=1.0)
    a = ap.parse_args()
    ms = MajorsSettings(leverage=a.leverage)
    s = load_settings()
    con = data.connect(ms.db_path)
    ts_list, bars, funding, real = data.load(con, ms.symbols)
    if len(ts_list) < 24 * 365:
        print("Not enough price history. Run first: py -m desk.majors.data")
        return
    prog = lambda k, n: print(f"\r  running {k}/{n}…", end="", file=sys.stderr, flush=True)  # noqa: E731
    wf = walk_forward(ts_list, bars, funding, ms, s, prog)
    print("\r" + " " * 30 + "\r", end="", file=sys.stderr)
    lines = report(wf, ms, real)
    print("\n".join(lines))
    best = max(wf["results"], key=lambda f: wf["results"][f]["sharpe"])
    _, fam, params, sh, atr_k = wf["schedules"][best][-1]
    Path(ms.db_path).with_name("majors_choice.json").write_text(json.dumps(
        {"family": fam, "params": list(params), "allow_short": sh, "atr_k": atr_k, "leverage": ms.leverage}, indent=1))


if __name__ == "__main__":
    main()
