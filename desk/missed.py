"""(3) Missed check: every skipped coin is rechecked 6 hours later. Did a filter cost us a winner?

The recorder stops tracking at 4h, so the 6h price is fetched from DexScreener (batched, 30 per call).
Run:  py -m desk.missed     summary of at most 20 lines (which skip reasons missed the most winners)
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from typing import Awaitable, Callable

from . import db, fields
from .config import load_settings
from .upgrade_settings import UpgradeSettings, load_upgrade_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS missed (
    mint TEXT PRIMARY KEY, symbol TEXT, skip_ts REAL, reason TEXT, skip_price REAL,
    check_ts REAL, check_price REAL);
"""
DEX_BATCH = 30


def record(con, mint: str, symbol: str, ts: float, reason: str, price: float | None) -> None:
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO missed (mint, symbol, skip_ts, reason, skip_price) VALUES (?,?,?,?,?)",
                (mint, symbol, ts, reason, price))
    con.commit()


def due(con, now: float, u: UpgradeSettings) -> list[str]:
    con.executescript(SCHEMA)
    return [r[0] for r in con.execute("SELECT mint FROM missed WHERE check_ts IS NULL AND skip_ts <= ?",
                                      (now - u.missed_after_s,))]


async def run_checks(con, now: float, u: UpgradeSettings,
                     fetch: Callable[[list[str]], Awaitable[list[dict]]]) -> int:
    """Fetch current prices for due coins (DexScreener pair list per batch). Returns how many were checked."""
    mints = due(con, now, u)
    for i in range(0, len(mints), DEX_BATCH):
        batch = mints[i:i + DEX_BATCH]
        pairs = await fetch(batch)
        for m in batch:
            pair = fields.pick_pair(pairs or [], m)
            price = fields.parse_pair(pair)["price"] if pair else 0.0     # no pair any more = dead
            con.execute("UPDATE missed SET check_ts=?, check_price=? WHERE mint=?", (now, price, m))
        con.commit()
    return len(mints)


def summary(con, u: UpgradeSettings) -> list[str]:
    con.executescript(SCHEMA)
    rows = con.execute("SELECT * FROM missed WHERE check_ts IS NOT NULL AND skip_price > 0").fetchall()
    pending = con.execute("SELECT COUNT(*) FROM missed WHERE check_ts IS NULL").fetchone()[0]
    if not rows:
        return [f"MISSED CHECK · nothing checked yet ({pending} coins waiting for their 6h recheck)"]
    by = defaultdict(lambda: [0, 0, 0])            # reason -> [checked, winners, dead]
    best = []
    for r in rows:
        chg = (r["check_price"] / r["skip_price"] - 1) * 100
        key = r["reason"].split(" ")[0] if not r["reason"].startswith(("audit", "market")) \
            else " ".join(r["reason"].split(" ")[:2]).rstrip(":")
        by[key][0] += 1
        by[key][1] += chg >= u.missed_winner_pct
        by[key][2] += r["check_price"] <= 0 or chg <= -90
        best.append((chg, r["symbol"] or r["mint"][:6], r["reason"]))
    winners = sum(v[1] for v in by.values())
    out = [f"MISSED CHECK · {len(rows)} skipped coins rechecked 6h later ({pending} waiting)",
           f"Missed winners (≥ +{u.missed_winner_pct:.0f}%): {winners} · dead/−90%: {sum(v[2] for v in by.values())}",
           "By skip reason (checked · winners · dead):"]
    for k, v in sorted(by.items(), key=lambda kv: -kv[1][0])[:9]:
        out.append(f"  {k[:28]:<28} {v[0]:>4} · {v[1]:>3} · {v[2]:>3}")
    out.append("Biggest misses:")
    for chg, sym, why in sorted(best, reverse=True)[:5]:
        out.append(f"  {sym[:10]:<10} {chg:+6.0f}%  skipped for: {why[:40]}")
    return out[:20]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    a = ap.parse_args()
    s = load_settings()
    con = db.connect(a.db or s.db_path)
    print("\n".join(summary(con, load_upgrade_settings())))


if __name__ == "__main__":
    main()
