"""Four simple, well-known strategies. Each sees one coin's hourly bars, one at a time
(no look-ahead), and says where it wants to be: +1 long, -1 short, 0 flat.
With allow_short=False a short signal means flat."""
from __future__ import annotations

from collections import deque


class RollingExtreme:
    """Max (or min) of the last n values in O(1) amortized."""

    def __init__(self, n: int, kind: str):
        self.n, self.sign = n, (1 if kind == "max" else -1)
        self.q: deque = deque()
        self.i = 0

    def push(self, v: float) -> None:
        while self.q and self.q[-1][1] * self.sign <= v * self.sign:
            self.q.pop()
        self.q.append((self.i, v))
        self.i += 1
        while self.q[0][0] <= self.i - 1 - self.n:
            self.q.popleft()

    @property
    def value(self) -> float | None:
        return self.q[0][1] if self.i >= self.n else None


class Strategy:
    family = "base"

    def __init__(self, allow_short: bool):
        self.allow_short = allow_short

    def _dir(self, d: int) -> int:
        return d if (d >= 0 or self.allow_short) else 0

    def update(self, bar: tuple, side: int, held: int, ts: int) -> int:
        raise NotImplementedError

    @property
    def name(self) -> str:
        return f"{self.family}{'' if self.allow_short else ' (long only)'}"


class TrendEMA(Strategy):
    """Trend following: long while the fast average is above the slow one, short below."""
    family = "trend"

    def __init__(self, fast: int, slow: int, allow_short: bool):
        super().__init__(allow_short)
        self.fast, self.slow, self.n = fast, slow, 0
        self.ef = self.es = None
        self.af, self.as_ = 2 / (fast + 1), 2 / (slow + 1)

    def update(self, bar, side, held, ts):
        c = bar[3]
        self.ef = c if self.ef is None else self.ef + self.af * (c - self.ef)
        self.es = c if self.es is None else self.es + self.as_ * (c - self.es)
        self.n += 1
        if self.n < self.slow:
            return 0
        return self._dir(1 if self.ef > self.es else -1)


class Breakout(Strategy):
    """Donchian breakout: long on a new N-hour high, short on a new N-hour low; exit on the shorter channel."""
    family = "breakout"

    def __init__(self, n_entry: int, n_exit: int, allow_short: bool):
        super().__init__(allow_short)
        self.hi, self.lo = RollingExtreme(n_entry, "max"), RollingExtreme(n_entry, "min")
        self.xhi, self.xlo = RollingExtreme(n_exit, "max"), RollingExtreme(n_exit, "min")

    def update(self, bar, side, held, ts):
        _, h, l, c = bar
        hi, lo, xhi, xlo = self.hi.value, self.lo.value, self.xhi.value, self.xlo.value   # previous bars only
        for r, v in ((self.hi, h), (self.lo, l), (self.xhi, h), (self.xlo, l)):
            r.push(v)
        if hi is None:
            return 0
        if c > hi:
            return self._dir(1)
        if c < lo:
            return self._dir(-1)
        if side > 0 and xlo is not None and c < xlo:
            return 0
        if side < 0 and xhi is not None and c > xhi:
            return 0
        return side


class RSIRevert(Strategy):
    """Mean reversion: buy when RSI is very low, short when very high, exit back at 50 or after max_hold hours."""
    family = "mean reversion"

    def __init__(self, lo: float, hi: float, max_hold: int, allow_short: bool, n: int = 14):
        super().__init__(allow_short)
        self.lo, self.hi, self.max_hold, self.n = lo, hi, max_hold, n
        self.prev = None
        self.ag = self.al = 0.0
        self.k = 0

    def update(self, bar, side, held, ts):
        c = bar[3]
        if self.prev is not None:
            ch = c - self.prev
            g, l = max(ch, 0.0), max(-ch, 0.0)
            self.k += 1
            if self.k <= self.n:
                self.ag += g / self.n
                self.al += l / self.n
            else:
                self.ag = (self.ag * (self.n - 1) + g) / self.n
                self.al = (self.al * (self.n - 1) + l) / self.n
        self.prev = c
        if self.k < self.n:
            return 0
        rsi = 100.0 if self.al == 0 else 100 - 100 / (1 + self.ag / self.al)
        if side != 0:
            if held >= self.max_hold or (side > 0 and rsi >= 50) or (side < 0 and rsi <= 50):
                return 0
            return side
        if rsi < self.lo:
            return 1
        if rsi > self.hi:
            return self._dir(-1)
        return 0


class TSMomentum(Strategy):
    """Time-series momentum: once a day, long if the coin is up over the lookback, short if down."""
    family = "momentum"

    def __init__(self, lookback: int, allow_short: bool):
        super().__init__(allow_short)
        self.closes: deque = deque(maxlen=lookback + 1)
        self.target = 0

    def update(self, bar, side, held, ts):
        self.closes.append(bar[3])
        if len(self.closes) == self.closes.maxlen and ts % 86400 == 82800:   # decide at the last hour of the day
            self.target = self._dir(1 if self.closes[-1] > self.closes[0] else -1)
        return self.target


FAMILIES = {
    "trend": (TrendEMA, [(20, 100), (50, 200), (100, 400)]),
    "breakout": (Breakout, [(240, 120), (480, 240), (720, 360)]),
    "mean reversion": (RSIRevert, [(25, 75, 48), (20, 80, 48), (30, 70, 24)]),
    "momentum": (TSMomentum, [(168,), (720,), (2160,)]),
}


def make(family: str, params: tuple, allow_short: bool) -> Strategy:
    cls, _ = FAMILIES[family]
    return cls(*params, allow_short=allow_short)


def combos() -> list[tuple[str, tuple, bool]]:
    return [(f, p, sh) for f, (_, ps) in FAMILIES.items() for p in ps for sh in (False, True)]
