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


def interp(a: Snap, b: Snap, ts: float) -> Snap:
    """Price/liquidity at time ts between snapshots a and b (a.ts <= ts <= b.ts)."""
    if b.ts == ts or b.ts <= a.ts:
        return b
    if b.liq < 1 or b.price <= 0:          # pool vanished — no interpolation into a rug
        return b
    w = (ts - a.ts) / (b.ts - a.ts)
    return Snap(ts, a.price + (b.price - a.price) * w, a.liq + (b.liq - a.liq) * w)


def price_at(snaps: list[Snap], ts: float) -> Snap | None:
    """Linear interpolation of the path at time ts (None if past the end of the data)."""
    tss = [x.ts for x in snaps]
    i = bisect_left(tss, ts)
    if i >= len(snaps):
        return None
    if i == 0:
        return snaps[i]
    return interp(snaps[i - 1], snaps[i], ts)


def find_entry(snaps: list[Snap], t0: float, s: Settings) -> int | None:
    """Index of the snapshot where the entry signal fires, or None."""
    if s.entry_type == "momentum":
        return find_momentum_entry(snaps, t0, s)
    return find_dip_entry(snaps, t0, s)


def find_momentum_entry(snaps: list[Snap], t0: float, s: Settings) -> int | None:
    """Buy a breakout: a new post-graduation high, up mom_up over the last mom_window_s, liquidity holding."""
    high = 0.0
    liq_peak = 0.0
    j = 0
    for i, x in enumerate(snaps):
        if x.price <= 0:
            return None
        prev_high = high
        high = max(high, x.price)
        liq_peak = max(liq_peak, x.liq)
        age = x.ts - t0
        if age < s.min_age_s:
            continue
        if age > s.max_entry_age_s:
            return None
        while snaps[j].ts < x.ts - s.mom_window_s:
            j += 1
        base = snaps[j].price
        if (j < i and base > 0 and x.price >= prev_high and x.price / base - 1 >= s.mom_up
                and x.liq >= liq_peak * (1 - s.max_liq_drop_entry)):
            return i
    return None


def find_dip_entry(snaps: list[Snap], t0: float, s: Settings) -> int | None:
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


class Position:
    """One open position. Shared by the backtest and paper trading so both follow identical rules.
    Feed it snapshots in time order with update(); decisions execute exec_delay_s later."""

    def __init__(self, mint: str, symbol: str, t0: float, fill: Snap, s: Settings):
        self.s, self.t0 = s, t0
        ticket = s.ticket_usd
        buy_slip = slippage(ticket, fill.liq, "buy", fill.ts - t0, s)
        entry_px = fill.price * (1 + buy_slip)              # effective price paid
        self.tokens = (ticket - fomo_fee(ticket)) / entry_px
        self.fill_price = fill.price
        self.fill_ts = fill.ts
        self.peak = fill.price
        self.tp_done = False
        self.prev_liq = fill.liq
        self.last = fill
        self.pending: tuple[str, float, float] | None = None   # (why, share of tokens, due ts)
        self.closed = False
        self.stop_px: float | None = None       # daily rule: tightened stop price
        self.trail_override: float | None = None  # daily rule: tighter trail, active immediately
        self.trade = Trade(mint, symbol, fill.ts, fill.ts, entry_px, ticket, 0.0, "")

    def _sell(self, n_tokens: float, at: Snap, why: str) -> None:
        t, s = self.trade, self.s
        self.tokens -= n_tokens
        t.exit_ts = at.ts
        if at.liq < s.rug_liq_usd or at.price <= 0:
            t.legs.append((at.ts, why + " (rugged)", 0.0))
            return
        gross = n_tokens * at.price
        gross *= 1 - slippage(gross, at.liq, "sell", at.ts - self.t0, s)
        net = max(0.0, gross - fomo_fee(gross))
        t.proceeds_usd += net
        t.legs.append((at.ts, why, net))

    def _execute(self, why: str, share: float, at: Snap) -> list[str]:
        self._sell(self.tokens * share, at, why)
        if share >= 1:
            self.closed = True
            self.trade.exit_reason = why
            return [why]
        return [why]

    def update(self, x: Snap) -> list[str]:
        """Process the next snapshot. Returns the names of any sells executed."""
        if self.closed or x.ts <= self.last.ts:
            return []
        s, events = self.s, []
        if self.pending and x.ts >= self.pending[2]:
            why, share, due = self.pending
            self.pending = None
            events += self._execute(why, share, interp(self.last, x, due))
            if self.closed:
                self.last = x
                return events
        self.peak = max(self.peak, x.price)
        why = None
        if x.liq < s.rug_liq_usd or x.price <= 0:
            why = "rug"
        elif x.liq < self.prev_liq * (1 - s.liq_pull_drop):
            why = "liquidity pulled"
        elif x.price <= max(self.fill_price * (1 - s.stop), getattr(self, "stop_px", None) or 0.0):
            why = "stop"
        elif getattr(self, "trail_override", None) and x.price <= self.peak * (1 - self.trail_override):
            why = "day trail"
        elif self.tp_done and x.price <= self.peak * (1 - s.trail):
            why = "trail"
        elif x.ts - self.fill_ts >= s.time_stop_s:
            why = "time"
        elif not self.tp_done and x.price >= self.fill_price * (1 + s.tp):
            self.tp_done = True
            self.pending = ("take profit", s.tp_sell_frac, x.ts + s.exec_delay_s)
        self.prev_liq = x.liq
        self.last = x
        if why == "rug":
            self.pending = None
            events += self._execute("rug", 1.0, x)
        elif why:
            self.pending = (why, 1.0, x.ts + s.exec_delay_s)
        return events

    def finish(self, why: str, at: Snap | None = None) -> list[str]:
        """Force-close (data ended, or no data for too long)."""
        if self.closed:
            return []
        at = at or self.last
        events = []
        if self.pending:
            pw, share, _ = self.pending
            self.pending = None
            events += self._execute(pw, share, at)
            if self.closed:
                return events
        return events + self._execute(why, 1.0, at)

    # --- persistence for paper trading ---
    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("s", "trade", "last")}
        d["last"] = [self.last.ts, self.last.price, self.last.liq]
        d["trade"] = self.trade.__dict__
        return d

    @classmethod
    def from_dict(cls, d: dict, s: Settings) -> "Position":
        p = cls.__new__(cls)
        p.__dict__.update({k: v for k, v in d.items() if k not in ("last", "trade")})
        p.s = s
        p.last = Snap(*d["last"])
        p.pending = tuple(d["pending"]) if d["pending"] else None
        p.stop_px = d.get("stop_px")
        p.trail_override = d.get("trail_override")
        p.trade = Trade(**d["trade"])
        return p


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
    pos = Position(mint, symbol, t0, fill, s)
    for x in snaps:
        if x.ts > fill.ts:
            pos.update(x)
            if pos.closed:
                return pos.trade, "traded"
    pos.finish("data end")
    return pos.trade, "traded"
