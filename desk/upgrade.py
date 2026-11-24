"""Plugs the audit-upgrade modules into paper trading as EXTRA filters before a trade.

Nothing in strategy.py / ledger.py / costs.py / paper.py changes. UpgradedPaperDesk subclasses PaperDesk:
- before a buy:  8-layer audit + market mode (only checked when the old rules would buy)
- every skip:    recorded for the 6-hour missed check
- every close:   realistic extra costs charged before the money is booked
"""
from __future__ import annotations

import json

from . import audit as audit_mod
from . import missed, realcosts
from .config import Settings
from .market import MarketMode
from .paper import STALE_SIGNAL_S, PaperDesk
from .strategy import Position, Snap, check_filters, find_entry
from .upgrade_settings import UpgradeSettings, load_upgrade_settings


def extra_checks(raw: dict | None, age_s: float, ts: float, market: MarketMode | None,
                 u: UpgradeSettings) -> str | None:
    """The only gate the upgrade adds. Returns a skip reason, or None to allow the trade."""
    why = audit_mod.audit_reason(audit_mod.audit(raw, age_s, u), u)
    if why:
        return why
    if market is not None:
        return market.blocked(ts)
    return None


class UpgradedPaperDesk(PaperDesk):
    def __init__(self, s: Settings, u: UpgradeSettings | None = None, market: MarketMode | None = None, **kw):
        self.u = u or load_upgrade_settings()
        self.market = market
        self._raw: dict[str, dict] = {}
        super().__init__(s, **kw)
        self.con.executescript(missed.SCHEMA)

    def _raw_report(self, mint: str) -> dict | None:
        if mint not in self._raw:
            row = self.con.execute("SELECT raw_json FROM rugcheck WHERE mint=? ORDER BY ts LIMIT 1", (mint,)).fetchone()
            if not row:
                return None
            self._raw[mint] = json.loads(row[0])
        return self._raw[mint]

    def _on_entry_snapshot(self, c, x: Snap, now: float) -> list[str]:
        # Only intervene at the exact moment the OLD rules would decide to buy.
        if not c.decided and c.pending_fill is None and x.ts - c.t0 <= self.s.max_entry_age_s:
            i = find_entry(c.snaps, c.t0, self.s)
            if (i is not None and i == len(c.snaps) - 1 and now - x.ts <= STALE_SIGNAL_S
                    and check_filters(c.rug, x, self.s) is None):
                why = extra_checks(self._raw_report(c.mint), x.ts - c.t0, x.ts, self.market, self.u)
                if why:
                    self._skip(c, why)
                    return []
        return super()._on_entry_snapshot(c, x, now)

    def _skip(self, c, why: str) -> None:
        if not why.startswith("signal missed"):
            price = c.snaps[-1].price if c.snaps else None
            missed.record(self.con, c.mint, c.symbol, c.snaps[-1].ts if c.snaps else 0.0, why, price)
        super()._skip(c, why)

    def _close(self, p: Position) -> list[str]:
        c = self.coins.get(p.trade.mint)
        realcosts.apply(p.trade, c.snaps if c else [], self.u)
        return super()._close(p)
