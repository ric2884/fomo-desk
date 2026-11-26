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
        self.banked = 0.0                                  # profit set aside by the daily rule; never traded
        self.day_start: dict[str, float] = {}
        self.day_banked: dict[str, float] = defaultdict(float)
        self.week_start: dict[str, float] = {}
        self.cur_week = ""
        self.week_banks: list[tuple[str, float]] = []          # (week, amount banked)

    @property
    def equity(self) -> float:
        """Tradable money: cash + money in open positions (at cost). Excludes banked profit."""
        return self.cash + sum(self.open_cost.values())

    @property
    def total(self) -> float:
        """The whole account: tradable + banked."""
        return self.equity + self.banked

    def _day(self, ts: float) -> str:
        w = week_of(ts)
        if w != self.cur_week:                                 # new week: bank last week's profit (weekly mode)
            prev = self.cur_week
            if prev and self.s.bank_mode == "weekly":
                gain = self.week_pnl[prev]
                if gain >= self.s.week_bank_pct * self.week_start[prev]:
                    amt = min(self.cash, gain)
                    if amt > 0:
                        self.cash -= amt
                        self.banked += amt
                        self.week_banks.append((prev, amt))
            self.cur_week = w
            self.week_start.setdefault(w, self.total)
        d = day_of(ts)
        if d not in self.day_start:
            self.day_start[d] = self.total
        return d

    def day_state(self, ts: float) -> str:
        """'normal', 'locked' (day ≥ +1%: no new trades) or 'banked' (day ≥ +2%: profit set aside)."""
        d = self._day(ts)
        start = self.day_start[d]
        if self.s.bank_mode == "daily" and self.day_pnl[d] >= self.s.day_bank_pct * start:
            return "banked"
        if self.day_pnl[d] >= self.s.day_lock_pct * start:
            return "locked"
        return "normal"

    def block_reason(self, ts: float, cost: float) -> str | None:
        """Why a new buy is not allowed right now (None = allowed). Order matters for reporting."""
        s = self.s
        if self.killed or self.total <= s.kill_switch_balance:
            self.killed = True
            return "kill switch"
        if self.day_state(ts) != "normal":
            return "day target reached"
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
        self._day(ts)
        if not self.equity_curve:
            self.equity_curve.append((ts, self.total))
        self.cash -= cost
        self.open_cost[mint] = cost
        self.day_count[day_of(ts)] += 1

    def on_close(self, mint: str, ts: float, proceeds: float) -> float:
        d = self._day(ts)
        cost = self.open_cost.pop(mint)
        self.cash += proceeds
        pnl = proceeds - cost
        self.day_pnl[d] += pnl
        self.week_pnl[week_of(ts)] += pnl
        if self.day_state(ts) == "banked":                 # bank today's realized profit not yet banked
            amt = min(self.cash, self.day_pnl[d] - self.day_banked[d])
            if amt > 0:
                self.cash -= amt
                self.banked += amt
                self.day_banked[d] += amt
        self.equity_curve.append((ts, self.total))
        if self.total <= self.s.kill_switch_balance:
            self.killed = True
        return pnl
