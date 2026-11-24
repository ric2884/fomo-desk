"""(2) Market mode: no new entries while BTC or SOL is falling fast.

Prices from CoinGecko's free API (no key). Live: one call per minute. Replay: hourly history,
so only the 1-hour rule can be checked historically (the 15-minute rule needs live data).
If market data is missing or stale, entries are NOT blocked (fail-open) — the reason is reported.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right

import httpx

from .upgrade_settings import UpgradeSettings

CG_NOW = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,solana&vs_currencies=usd"
CG_HIST = "https://api.coingecko.com/api/v3/coins/{}/market_chart?vs_currency=usd&days={}"
UA = {"User-Agent": "desk-market/1.0"}


class MarketMode:
    def __init__(self, u: UpgradeSettings):
        self.u = u
        self.ts: list[float] = []
        self.btc: list[float] = []
        self.sol: list[float] = []
        self.block_until = 0.0
        self.last_trigger = ""

    def add(self, ts: float, btc: float, sol: float) -> None:
        if self.ts and ts <= self.ts[-1]:
            return
        self.ts.append(ts)
        self.btc.append(btc)
        self.sol.append(sol)

    def _drop(self, series: list[float], ts: float, window_s: float) -> float | None:
        """Drop from the highest price in [ts - window, ts] to the price at ts (0.03 = −3%)."""
        hi_i = bisect_right(self.ts, ts)
        if hi_i == 0:
            return None
        lo_i = bisect_left(self.ts, ts - window_s)
        window = series[lo_i:hi_i]
        if len(window) < 2:
            return None
        return 1 - window[-1] / max(window)

    def blocked(self, ts: float) -> str | None:
        """Reason new entries are blocked at time ts, or None."""
        u = self.u
        rules = [("BTC", self.btc, 900, u.market_btc_drop_15m), ("SOL", self.sol, 900, u.market_sol_drop_15m),
                 ("BTC", self.btc, 3600, u.market_btc_drop_1h), ("SOL", self.sol, 3600, u.market_sol_drop_1h)]
        for name, series, win, limit in rules:
            d = self._drop(series, ts, win)
            if d is not None and d >= limit:
                self.block_until = max(self.block_until, ts + u.market_cooloff_s)
                self.last_trigger = f"{name} −{d * 100:.1f}% in {win // 60} min"
        if ts < self.block_until:
            return f"market: {self.last_trigger}"
        return None

    def stale(self, now: float) -> bool:
        return not self.ts or now - self.ts[-1] > 10 * self.u.market_poll_s


async def fetch_now(client: httpx.AsyncClient) -> tuple[float, float] | None:
    try:
        r = await client.get(CG_NOW, headers=UA, timeout=15)
        d = r.json()
        return float(d["bitcoin"]["usd"]), float(d["solana"]["usd"])
    except Exception:  # noqa: BLE001 — fail-open, caller reports staleness
        return None


def load_history(mm: MarketMode, days: int) -> bool:
    """Fill mm with CoinGecko history (hourly for 2–90 days). Returns False if unavailable."""
    try:
        series = {}
        for cid in ("bitcoin", "solana"):
            r = httpx.get(CG_HIST.format(cid, max(2, days)), headers=UA, timeout=30)
            series[cid] = {int(t // 1000): p for t, p in r.json()["prices"]}
    except Exception:  # noqa: BLE001
        return False
    sol = sorted(series["solana"].items())
    sol_ts = [t for t, _ in sol]
    for t, btc in sorted(series["bitcoin"].items()):
        j = bisect_left(sol_ts, t)
        if j < len(sol) and abs(sol_ts[j] - t) < 1800:
            mm.add(float(t), btc, sol[j][1])
    return len(mm.ts) > 10
