"""(4) Realistic costs: what costs.py does not charge yet. Applied to a finished trade.

costs.py already charges: FOMO fee per side, AMM price impact, early-buy slippage floor, 5 s delay.
This adds (all ESTIMATES — compare with real fills once live):
- network/priority fee per transaction (buy + every sell)
- MEV/sandwich loss on the buy (fewer tokens → smaller proceeds)
- extra sell slippage when the price is moving fast (selling into a dump fills worse)
"""
from __future__ import annotations

from bisect import bisect_left

from .strategy import Snap, Trade
from .upgrade_settings import UpgradeSettings


def _price_at(snaps: list[Snap], ts: float) -> float | None:
    if not snaps:
        return None
    i = bisect_left([x.ts for x in snaps], ts)
    return snaps[min(i, len(snaps) - 1)].price


def extra_cost(t: Trade, snaps: list[Snap], u: UpgradeSettings) -> float:
    """Dollars to subtract from the trade's proceeds."""
    n_tx = 1 + len(t.legs)
    cost = u.network_fee_usd * n_tx
    cost += t.proceeds_usd * u.mev_buy_pct
    for leg in t.legs:
        ts, net = leg[0], leg[2]
        if net <= 0:
            continue
        now, before = _price_at(snaps, ts), _price_at(snaps, ts - 15)
        if now and before:
            move = abs(now - before) / before
            cost += net * min(u.fast_move_cap, u.fast_move_factor * move)
    return min(cost, t.proceeds_usd + u.network_fee_usd * n_tx)


def apply(t: Trade, snaps: list[Snap], u: UpgradeSettings) -> float:
    """Charge the extra cost on the trade in place. Returns the amount charged."""
    c = extra_cost(t, snaps, u)
    t.proceeds_usd = t.proceeds_usd - c
    return c
