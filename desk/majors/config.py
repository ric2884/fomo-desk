"""Majors settings. Money limits (bankroll, daily/weekly loss, kill switch, daily profit rule)
come from the main desk Settings so both bots follow the same rules."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import ROOT

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT"]


@dataclass
class MajorsSettings:
    db_path: str = str(ROOT / "data" / "majors.db")
    symbols: list = field(default_factory=lambda: list(SYMBOLS))
    start: str = "2018-01-01"               # includes the 2018 and 2022 bear markets
    leverage: float = 1.0                    # default 1×; hard max 2× (enforced)
    fee: float = 0.0005                      # futures taker fee per side (0.05%)
    slip: dict = field(default_factory=lambda: {"BTCUSDT": 0.0002, "ETHUSDT": 0.0002})
    slip_default: float = 0.0005             # other coins
    mmr: float = 0.005                       # maintenance margin (liquidation)
    funding_default: float = 0.0001          # per 8h, used only if real funding history is unavailable
    tighten_pct: float = 0.01                # day +1%: losing positions get a stop 1% away
    bank_trail: float = 0.02                 # day +2%: winners trail 2% behind their best price
    oos_start: str = "2020-01-01"            # walk-forward: unseen periods start here
    atr_n: int = 14                          # volatility (ATR) over 14 hours
    atr_k: float = 0.0                       # stop-loss distance in ATRs (0 = off); the backtest picks 0, 3 or 6
    fold_months: int = 6                     # settings re-picked every 6 months
    warm_bars: int = 2500                    # indicator warm-up before a strategy may trade

    def __post_init__(self):
        if not 1.0 <= self.leverage <= 2.0:
            raise ValueError(f"leverage must be between 1× and 2×, not {self.leverage}")

    def slippage(self, sym: str) -> float:
        return self.slip.get(sym, self.slip_default)
