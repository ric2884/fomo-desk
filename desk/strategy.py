"""The strategy: filters, entry, exits — simulated on a recorded price path with full costs.
The same rules will later drive paper and live trading."""
from __future__ import annotations

import random
from bisect import bisect_left
from dataclasses import dataclass, field

from .costs import fomo_fee, slippage
from .config import Settings


@dataclass
class Snap:
    ts: float
    price: float
    liq: float


@dataclass
class Trade:
    mint: str
    symbol: str
    entry_ts: float
    exit_ts: float
    entry_price: float
    cost_usd: float
    proceeds_usd: float
    exit_reason: str
    legs: list = field(default_factory=list)

    @property
    def pnl(self) -> float:
        return self.proceeds_usd - self.cost_usd

    @property
    def pnl_pct(self) -> float:
        return self.pnl / self.cost_usd * 100


def check_filters(rug: dict | None, snap: Snap, s: Settings) -> str | None:
    """Returns None if all filters pass, else the reason for skipping."""
    if rug is None:
        return "no rugcheck report"
    if rug["rugged"]:
        return "rugcheck: rugged"
    if not rug["mint_revoked"]:
        return "mint not revoked"
    if not rug["freeze_revoked"]:
        return "freeze not revoked"
    if rug["lp_locked_pct"] is None or rug["lp_locked_pct"] < s.min_lp_locked_pct:
        return f"lp locked {rug['lp_locked_pct']}%"
    if rug["top10_pct"] is None or rug["top10_pct"] > s.max_top10_pct:
        return f"top10 {rug['top10_pct']:.1f}%" if rug["top10_pct"] is not None else "top10 unknown"
    if rug["creator_pct"] is not None and rug["creator_pct"] > s.max_creator_pct:
        return f"creator holds {rug['creator_pct']:.1f}%"
    if s.skip_danger_risks and rug["danger_risks"]:
        return "danger: " + ", ".join(rug["danger_risks"][:3])
    if snap.liq < s.min_liq_usd:
        return f"liquidity ${snap.liq:,.0f}"
    return None


def price_at(snaps: list[Snap], ts: float) -> Snap | None:
    """Linear interpolation of the path at time ts (None if past the end of the data)."""
    tss = [x.ts for x in snaps]
    i = bisect_left(tss, ts)
    if i >= len(snaps):
        return None
    if i == 0 or snaps[i].ts == ts:
        return snaps[i]
    a, b = snaps[i - 1], snaps[i]
    w = (ts - a.ts) / (b.ts - a.ts)
    if b.liq < 1 or b.price <= 0:          # pool vanished — no interpolation into a rug
        return b
    return Snap(ts, a.price + (b.price - a.price) * w, a.liq + (b.liq - a.liq) * w)


def find_entry(snaps: list[Snap], t0: float, s: Settings) -> int | None:
    """Index of the snapshot where the dip-entry signal fires, or None."""
    high = 0.0
    liq_peak = 0.0
    for i, x in enumerate(snaps):
        age = x.ts - t0
        if x.price <= 0:
            return None
        high = max(high, x.price)
        liq_peak = max(liq_peak, x.liq)
        if age < s.min_age_s:
            continue
        if age > s.max_entry_age_s:
            return None
        dip = 1 - x.price / high
        if s.dip_min <= dip <= s.dip_max and x.liq >= liq_peak * (1 - s.max_liq_drop_entry):
            return i
    return None


def simulate(mint: str, symbol: str, t0: float, snaps: list[Snap], rug: dict | None,
             s: Settings, rng: random.Random) -> tuple[Trade | None, str]:
    snaps = [x for x in snaps if x.price is not None and x.liq is not None]
    if len(snaps) < 3:
        return None, "not enough data"
    i = find_entry(snaps, t0, s)
    if i is None:
        return None, "no dip entry"
    reason = check_filters(rug, snaps[i], s)
    if reason:
        return None, reason
    if rng.random() < s.fail_rate:
        return None, "buy failed (simulated)"

    fill = price_at(snaps, snaps[i].ts + s.exec_delay_s)
    if fill is None or fill.price <= 0 or fill.liq < s.rug_liq_usd:
        return None, "no fill"
    ticket = s.ticket_usd
    buy_slip = slippage(ticket, fill.liq, "buy", fill.ts - t0, s)
    entry_px = fill.price * (1 + buy_slip)                 # effective price paid
    tokens = (ticket - fomo_fee(ticket)) / entry_px
    t = Trade(mint, symbol, fill.ts, fill.ts, entry_px, ticket, 0.0, "")

    def sell(n_tokens: float, at: Snap, why: str) -> None:
        if at.liq < s.rug_liq_usd or at.price <= 0:
            t.legs.append((at.ts, why + " (rugged)", 0.0))
            return
        gross = n_tokens * at.price
        gross *= 1 - slippage(gross, at.liq, "sell", at.ts - t0, s)
        net = max(0.0, gross - fomo_fee(gross))
        t.proceeds_usd += net
        t.legs.append((at.ts, why, net))
        t.exit_ts = at.ts

    peak = fill.price
    tp_done = False
    prev_liq = fill.liq
    later = [x for x in snaps if x.ts > fill.ts]
    for x in later:
        peak = max(peak, x.price)
        why = None
        if x.liq < s.rug_liq_usd or x.price <= 0:
            why = "rug"
        elif x.liq < prev_liq * (1 - s.liq_pull_drop):
            why = "liquidity pulled"
        elif x.price <= fill.price * (1 - s.stop):
            why = "stop"
        elif tp_done and x.price <= peak * (1 - s.trail):
            why = "trail"
        elif x.ts - fill.ts >= s.time_stop_s:
            why = "time"
        elif not tp_done and x.price >= fill.price * (1 + s.tp):
            ex = price_at(snaps, x.ts + s.exec_delay_s) or x
            part = tokens * s.tp_sell_frac
            sell(part, ex, "take profit")
            tokens -= part
            tp_done = True
        prev_liq = x.liq
        if why:
            ex = x if why == "rug" else (price_at(snaps, x.ts + s.exec_delay_s) or x)
            sell(tokens, ex, why)
            t.exit_reason = why
            t.exit_ts = ex.ts
            return t, "traded"
    # recording ended with the position still open: close at the last known price
    sell(tokens, later[-1] if later else fill, "data end")
    t.exit_reason = "data end"
    return t, "traded"
