"""All tunable numbers live here. Override any of them in settings.json (same keys)."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    # --- recorder ---
    db_path: str = str(ROOT / "data" / "desk.db")
    fast_poll_s: int = 15           # snapshot interval for the first hour
    slow_poll_s: int = 60           # snapshot interval from 1h to 4h
    fast_window_s: int = 3600
    track_window_s: int = 4 * 3600
    rugcheck_delay_s: int = 90      # fetch RugCheck report this long after graduation
    dex_max_per_min: int = 250
    ws_silence_s: int = 120
    raw_samples: int = 20           # raw API responses kept per source for field checks

    # --- costs (STEP 2) ---
    slip_min: float = 0.015         # 1.5% per side minimum
    slip_impact_mult: float = 2.0   # slippage = ticket / liquidity * 200%
    slip_early_floor: float = 0.03  # buys within 5 min of graduation pay at least 3%
    slip_early_window_s: int = 300
    exec_delay_s: float = 5.0       # browser lane is slow
    fail_rate: float = 0.05         # share of buys that fail

    # --- strategy (STEP 3) — starting guesses, NOT proven ---
    entry_type: str = "dip"         # "dip" (buy the first dip) or "momentum" (buy a breakout)
    mom_up: float = 0.30            # momentum: price up this much over mom_window_s, at a new high
    mom_window_s: int = 120
    ticket_usd: float = 5.0
    min_ticket_usd: float = 5.0
    min_liq_usd: float = 20_000
    max_top10_pct: float = 30.0
    max_creator_pct: float = 5.0
    min_lp_locked_pct: float = 99.0
    skip_danger_risks: bool = True
    min_age_s: int = 120
    max_entry_age_s: int = 1800
    dip_min: float = 0.15
    dip_max: float = 0.35
    max_liq_drop_entry: float = 0.20
    stop: float = 0.20
    tp: float = 0.50
    tp_sell_frac: float = 0.50
    trail: float = 0.30
    time_stop_s: int = 45 * 60
    liq_pull_drop: float = 0.40
    rug_liq_usd: float = 1_000

    # --- money limits (STEP 7) ---
    bankroll_usd: float = 100.0
    max_open: int = 3
    max_trades_day: int = 15
    max_exposure_frac: float = 0.30
    daily_loss_limit: float = 8.0
    weekly_loss_limit: float = 20.0
    kill_switch_balance: float = 60.0

    # --- daily profit rule (target to measure against, NOT a promise) ---
    day_lock_pct: float = 0.01      # day up 1%: no new trades, tighten stops on open ones
    day_bank_pct: float = 0.02      # day up 2%: bank the profit, keep only winners with a tight trail
    day_lock_trail: float = 0.15    # memecoin trailing stop after the day is banked

    # --- backtest ---
    train_frac: float = 0.70
    min_trades_for_tuning: int = 20
    seed: int = 7


def load_settings(path: str | Path | None = None) -> Settings:
    s = Settings()
    p = Path(path) if path else ROOT / "settings.json"
    if p.exists():
        data = json.loads(p.read_text())
        known = {f.name for f in fields(Settings)}
        bad = set(data) - known
        if bad:
            raise ValueError(f"Unknown settings in {p}: {sorted(bad)}")
        for k, v in data.items():
            setattr(s, k, type(getattr(s, k))(v))
    if s.entry_type not in ("dip", "momentum"):
        raise ValueError(f"entry_type must be 'dip' or 'momentum', not {s.entry_type!r}")
    if s.ticket_usd < s.min_ticket_usd:
        raise ValueError(f"ticket_usd {s.ticket_usd} is below the ${s.min_ticket_usd} minimum")
    return s


def with_overrides(s: Settings, **kw) -> Settings:
    d = asdict(s)
    d.update(kw)
    return Settings(**d)
