"""Futures paper engine for the majors bot. Shared by the backtest and live paper trading.

Each hourly bar, in this order (no look-ahead):
 1. orders decided at the previous close fill at this bar's OPEN (+ slippage + taker fee)
 2. liquidation and stop checks against this bar's high/low (gaps fill at the open)
 3. funding every 8h (00/08/16 UTC): longs pay positive rates, shorts receive them
 4. mark to market at the close
 5. daily profit rule: day ≥ +1% → no new entries, stops tightened; ≥ +2% → losers closed,
    today's realized profit banked (never traded again), winners trail
 6. money limits: daily loss $8 / weekly $20 → no new entries; total ≤ $60 → close all, stop for good
 7. strategies decide at the close → orders for the next bar
Sizing: each coin gets (tradable equity × leverage ÷ number of coins) when a position opens.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..config import Settings
from ..ledger import day_of, week_of
from .config import MajorsSettings

FUNDING_EVERY = 8 * 3600


@dataclass
class Pos:
    side: int                 # +1 long, -1 short
    qty: float
    entry: float
    margin: float
    opened: int
    held: int = 0
    stop: float | None = None
    trail: float | None = None
    peak: float = 0.0         # best price since entry (high for longs, low for shorts)

    def liq_price(self, lev: float, mmr: float) -> float:
        return self.entry * (1 - 1 / lev + mmr) if self.side > 0 else self.entry * (1 + 1 / lev - mmr)

    def upnl(self, price: float) -> float:
        return self.side * self.qty * (price - self.entry)


class Engine:
    def __init__(self, ms: MajorsSettings, s: Settings, strategies: dict, funding: dict | None = None,
                 limits: bool = True):
        self.ms, self.s, self.strats = ms, s, strategies
        self.funding = funding or {}
        self.limits = limits
        self.cash = s.bankroll_usd
        self.banked = 0.0
        self.pos: dict[str, Pos] = {}
        self.pending: dict[str, int] = {}
        self.last: dict[str, float] = {}
        self.killed = False
        self.day = self.week = ""
        self.day_start = s.bankroll_usd
        self.day_realized = 0.0
        self.day_banked = 0.0
        self.day_state = "normal"
        self.day_hit = False
        self.week_realized = 0.0
        self.days: list[tuple[str, float, float, bool]] = []   # (day, start total, end total, reached +1%)
        self.last_ts = 0
        self.fees = self.funding_paid = 0.0
        self.trades = 0

    # ---------- money ----------
    @property
    def total(self) -> float:
        return self.cash + self.banked + sum(p.margin + p.upnl(self.last.get(sym, p.entry))
                                             for sym, p in self.pos.items())

    @property
    def tradable(self) -> float:
        return self.total - self.banked

    def _realize(self, amount: float) -> None:
        self.day_realized += amount
        self.week_realized += amount
        if self.day_state == "banked":
            amt = min(self.cash, self.day_realized - self.day_banked)
            if amt > 0:
                self.cash -= amt
                self.banked += amt
                self.day_banked += amt

    def _slip(self, sym: str, price: float, side: int) -> float:
        """Fill price for an order in direction `side` (+1 buy, -1 sell)."""
        return price * (1 + side * self.ms.slippage(sym))

    def _open(self, sym: str, side: int, price: float, ts: int, ev: list) -> None:
        alloc = min(max(0.0, self.tradable) / len(self.ms.symbols), self.cash)
        notional = alloc / (1 / self.ms.leverage + self.ms.fee)      # margin + fee == this coin's allocation
        if notional < 1:
            return
        px = self._slip(sym, price, side)
        fee = notional * self.ms.fee
        margin = notional / self.ms.leverage
        if self.cash + 1e-6 < margin + fee:
            return
        self.cash -= margin + fee
        self.fees += fee
        self._realize(-fee)
        self.pos[sym] = Pos(side, notional / px, px, margin, ts, peak=px)
        self.trades += 1
        ev.append(f"{'Long' if side > 0 else 'Short'} {sym[:-4]} ${notional:.2f} @ {px:.4g}")

    def _close(self, sym: str, price: float, why: str, ev: list, slip: bool = True) -> None:
        p = self.pos.pop(sym)
        px = self._slip(sym, price, -p.side) if slip else price
        fee = p.qty * px * self.ms.fee
        pnl = p.side * p.qty * (px - p.entry)
        self.cash += p.margin + pnl - fee
        self.fees += fee
        self._realize(pnl - fee)
        ev.append(f"Closed {'long' if p.side > 0 else 'short'} {sym[:-4]} {pnl - fee:+.2f} ({why})")

    def _entries_allowed(self) -> bool:
        if self.killed or self.day_state != "normal":
            return False
        if self.limits and (self.day_realized <= -self.s.daily_loss_limit
                            or self.week_realized <= -self.s.weekly_loss_limit):
            return False
        return True

    # ---------- the hourly step ----------
    def on_bar(self, ts: int, bars: dict[str, tuple]) -> list[str]:
        ev: list[str] = []
        self._new_period(ts)

        # 1. fills at the open
        for sym, target in list(self.pending.items()):
            if sym not in bars:
                continue
            o = bars[sym][0]
            cur = self.pos.get(sym)
            if cur and cur.side != target:
                self._close(sym, o, "signal", ev)
            if target != 0 and sym not in self.pos and self._entries_allowed():
                self._open(sym, target, o, ts, ev)
            del self.pending[sym]

        # 2. liquidation and stops on the bar's range
        for sym, p in list(self.pos.items()):
            if sym not in bars:
                continue
            o, h, l, c = bars[sym]
            liq = p.liq_price(self.ms.leverage, self.ms.mmr)
            worst = l if p.side > 0 else h
            if (p.side > 0 and worst <= liq) or (p.side < 0 and worst >= liq):
                self.pos.pop(sym)
                self._realize(-p.margin)
                ev.append(f"LIQUIDATED {sym[:-4]} −${p.margin:.2f}")
                continue
            if p.stop is not None and ((p.side > 0 and l <= p.stop) or (p.side < 0 and h >= p.stop)):
                fill = min(o, p.stop) if p.side > 0 else max(o, p.stop)
                self._close(sym, fill, "trailing stop" if p.trail else "stop", ev)
                continue
            p.peak = max(p.peak, h) if p.side > 0 else min(p.peak, l)
            if p.trail:
                trail_stop = p.peak * (1 - p.trail) if p.side > 0 else p.peak * (1 + p.trail)
                p.stop = trail_stop if p.stop is None else (max(p.stop, trail_stop) if p.side > 0 else min(p.stop, trail_stop))
            p.held += 1

        # 3. funding
        if ts % FUNDING_EVERY == 0:
            for sym, p in self.pos.items():
                if sym in bars:
                    rate = self.funding.get(sym, {}).get(ts, self.ms.funding_default)
                    pay = p.qty * bars[sym][0] * rate * p.side
                    self.cash -= pay
                    self.funding_paid += pay
                    self._realize(-pay)

        # 4. mark
        for sym, b in bars.items():
            self.last[sym] = b[3]
        total = self.total

        # 5. daily profit rule
        day_pnl = total - self.day_start
        if self.day_state == "normal" and day_pnl >= self.s.day_lock_pct * self.day_start:
            self.day_state, self.day_hit = "locked", True
            for sym, p in self.pos.items():
                c = self.last[sym]
                if p.upnl(c) > 0:
                    p.stop = p.entry                                       # can't turn into a loss
                else:
                    p.stop = c * (1 - self.ms.tighten_pct) if p.side > 0 else c * (1 + self.ms.tighten_pct)
            ev.append(f"🎯 Day +{day_pnl / self.day_start:.1%}: target reached, no new trades, stops tightened")
        if self.day_state == "locked" and day_pnl >= self.s.day_bank_pct * self.day_start:
            for sym in [s_ for s_, p in self.pos.items() if p.upnl(self.last[s_]) <= 0]:
                self._close(sym, self.last[sym], "day banked: loser", ev)
            for p in self.pos.values():
                p.trail = self.ms.bank_trail
            self.day_state = "banked"
            self._realize(0.0)
            ev.append(f"🏦 Day +{day_pnl / self.day_start:.1%}: profit banked (total banked ${self.banked:.2f})")

        # 6. kill switch
        if self.limits and not self.killed and self.total <= self.s.kill_switch_balance:
            for sym in list(self.pos):
                self._close(sym, self.last[sym], "kill switch", ev)
            self.killed = True
            ev.append(f"🛑 Kill switch: total ${self.total:.2f}. Bot stopped.")

        # 7. decisions at the close
        for sym, b in bars.items():
            st = self.strats.get(sym)
            if st is None:
                continue
            p = self.pos.get(sym)
            target = st.update(b, p.side if p else 0, p.held if p else 0, ts)
            cur = p.side if p else 0
            if self.killed:
                continue
            if target != cur:
                if target != 0 and not self._entries_allowed():
                    target = 0                                             # may close, may not open
                if target != cur:
                    self.pending[sym] = target
        self.last_ts = ts
        return ev

    def _new_period(self, ts: int) -> None:
        d, w = day_of(ts), week_of(ts)
        if d != self.day:
            if self.day:
                self.days.append((self.day, self.day_start, self.total, self.day_hit))
            self.day, self.day_start = d, self.total
            self.day_realized = self.day_banked = 0.0
            self.day_state, self.day_hit = "normal", False   # yesterday's tightened stops/trails stay (protective)
        if w != self.week:
            self.week, self.week_realized = w, 0.0

    def finish(self) -> None:
        """Record the last (partial) day."""
        if self.day:
            self.days.append((self.day, self.day_start, self.total, self.day_hit))
            self.day = ""

    # ---------- persistence (live paper) ----------
    def state(self) -> dict:
        keep = ("cash", "banked", "pending", "last", "killed", "day", "week", "day_start", "day_realized",
                "day_banked", "day_state", "day_hit", "week_realized", "days", "last_ts", "fees", "funding_paid", "trades")
        d = {k: getattr(self, k) for k in keep}
        d["pos"] = {sym: asdict(p) for sym, p in self.pos.items()}
        return d

    def restore(self, d: dict) -> None:
        for k, v in d.items():
            if k == "pos":
                self.pos = {sym: Pos(**p) for sym, p in v.items()}
            elif k == "days":
                self.days = [tuple(x) for x in v]
            else:
                setattr(self, k, v)
