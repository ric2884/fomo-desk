"""Settings for the audit-upgrade modules. Kept separate so config.py stays untouched.
Override any value in upgrade.json (same keys)."""
from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

from .config import ROOT


@dataclass
class UpgradeSettings:
    # --- 8-layer audit ---
    audit_max_dev_pct: float = 5.0          # layer 2: developer holding
    audit_max_top10_pct: float = 30.0       # layer 3: top-10 holders, pool excluded
    audit_min_lp_locked_pct: float = 99.0   # layer 4: liquidity locked/burned
    audit_min_pool_age_s: int = 180         # layer 5: pool age at the moment of the buy
    audit_max_linked_insiders: int = 3      # layer 6: RugCheck graphInsidersDetected
    audit_max_network_size: int = 10        # layer 6: biggest linked-wallet network
    audit_max_insider_pct: float = 10.0     # layer 7: % held by wallets RugCheck flags as insiders
    audit_unknown_blocks: bool = False      # a layer with no data: block (True) or let through (False)

    # --- market mode ---
    market_btc_drop_15m: float = 0.015
    market_sol_drop_15m: float = 0.025
    market_btc_drop_1h: float = 0.03
    market_sol_drop_1h: float = 0.05
    market_cooloff_s: int = 3600
    market_poll_s: int = 60

    # --- missed check ---
    missed_after_s: int = 6 * 3600
    missed_winner_pct: float = 50.0         # "missed winner" = up at least this much 6h later

    # --- realistic costs (on top of costs.py) — ESTIMATES, check against real fills later ---
    network_fee_usd: float = 0.02           # Solana base + priority fee per transaction
    mev_buy_pct: float = 0.005              # sandwich/MEV loss on buys
    fast_move_factor: float = 0.5           # extra sell slippage = factor × price move in the last 15 s
    fast_move_cap: float = 0.05


def load_upgrade_settings(path: str | Path | None = None) -> UpgradeSettings:
    u = UpgradeSettings()
    p = Path(path) if path else ROOT / "upgrade.json"
    if p.exists():
        data = json.loads(p.read_text())
        known = {f.name for f in fields(UpgradeSettings)}
        bad = set(data) - known
        if bad:
            raise ValueError(f"Unknown upgrade settings in {p}: {sorted(bad)}")
        for k, v in data.items():
            setattr(u, k, type(getattr(u, k))(v))
    return u
