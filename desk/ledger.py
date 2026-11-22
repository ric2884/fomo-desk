"""STEP 7 — money limits. Shared by backtest, paper and (later) live, so all three behave the same."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

from .config import Settings


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def week_of(ts: float) -> str:
    y, w, _ = datetime.fromtimestamp(ts, timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


class Ledger:
    def __init__(self, s: Settings):
        self.s = s
        self.cash = s.bankroll_usd
        self.open_cost: dict[str, float] = {}
        self.day_pnl: dict[str, float] = defaultdict(float)
        self.week_pnl: dict[str, float] = defaultdict(float)
        self.day_count: Counter = Counter()
        self.killed = False
        self.equity_curve: list[tuple[float, float]] = []

    @property
    def equity(self) -> float:
        return self.cash + sum(self.open_cost.values())

    def block_reason(self, ts: float, cost: float) -> str | None:
        """Why a new buy is not allowed right now (None = allowed). Order matters for reporting."""
        s = self.s
        if self.killed or self.equity <= s.kill_switch_balance:
            self.killed = True
            return "kill switch"
        if self.day_pnl[day_of(ts)] <= -s.daily_loss_limit:
            return "daily loss limit"
        if self.week_pnl[week_of(ts)] <= -s.weekly_loss_limit:
            return "weekly loss limit"
        if len(self.open_cost) >= s.max_open:
            return "max open positions"
        if self.day_count[day_of(ts)] >= s.max_trades_day:
            return "max trades/day"
        if self.cash < cost or sum(self.open_cost.values()) + cost > s.max_exposure_frac * self.equity:
            return "not enough cash / exposure"
        return None

    def on_open(self, mint: str, ts: float, cost: float) -> None:
        if not self.equity_curve:
            self.equity_curve.append((ts, self.equity))
        self.cash -= cost
        self.open_cost[mint] = cost
        self.day_count[day_of(ts)] += 1

    def on_close(self, mint: str, ts: float, proceeds: float) -> float:
        cost = self.open_cost.pop(mint)
        self.cash += proceeds
        pnl = proceeds - cost
        self.day_pnl[day_of(ts)] += pnl
        self.week_pnl[week_of(ts)] += pnl
        self.equity_curve.append((ts, self.equity))
        if self.equity <= self.s.kill_switch_balance:
            self.killed = True
        return pnl
