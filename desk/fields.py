"""The ONLY place that knows the shape of external API responses.
If an API changes, fix it here. Unknown values come back as None — never guessed."""
from __future__ import annotations

from typing import Any


def parse_migration(msg: dict) -> dict | None:
    """PumpPortal `subscribeMigration` event -> {mint, pool, signature} or None."""
    if not isinstance(msg, dict):
        return None
    mint = msg.get("mint")
    if not mint or msg.get("txType", "migrate") != "migrate":
        return None
    return {"mint": mint, "pool": msg.get("pool"), "signature": msg.get("signature")}


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def pick_pair(pairs: list[dict], mint: str) -> dict | None:
    """Among DexScreener pairs, pick the PumpSwap pool for this mint, else the deepest one."""
    mine = [p for p in pairs or [] if (p.get("baseToken") or {}).get("address") == mint]
    if not mine:
        return None
    pump = [p for p in mine if p.get("dexId") == "pumpswap"]
    pool = pump or mine
    return max(pool, key=lambda p: _f((p.get("liquidity") or {}).get("usd")) or 0)


def parse_pair(pair: dict) -> dict:
    txm5 = (pair.get("txns") or {}).get("m5") or {}
    base = pair.get("baseToken") or {}
    return {
        "price": _f(pair.get("priceUsd")),
        "liq": _f((pair.get("liquidity") or {}).get("usd")),
        "mcap": _f(pair.get("marketCap") or pair.get("fdv")),
        "vol_m5": _f((pair.get("volume") or {}).get("m5")),
        "buys_m5": txm5.get("buys"),
        "sells_m5": txm5.get("sells"),
        "name": base.get("name"),
        "symbol": base.get("symbol"),
    }


def _addresses(v: Any) -> set[str]:
    """Solana addresses inside a value that may be a string, a dict, or a list (API shapes vary)."""
    if isinstance(v, str):
        return {v} if 32 <= len(v) <= 44 else set()
    if isinstance(v, dict):
        return set().union(*(_addresses(x) for x in v.values() if isinstance(x, str)), set())
    if isinstance(v, list):
        return set().union(*(_addresses(x) for x in v), set())
    return set()


def parse_rugcheck(r: dict) -> dict:
    """RugCheck /v1/tokens/{mint}/report -> the facts our filters use."""
    markets = r.get("markets") or []
    pool_accounts: set[str] = set()
    lp_locked = []
    for m in markets:
        for k in ("pubkey", "liquidityAAccount", "liquidityBAccount", "liquidityA", "liquidityB"):
            pool_accounts |= _addresses(m.get(k))
        lp = m.get("lp") or {}
        if lp.get("lpLockedPct") is not None:
            lp_locked.append(_f(lp["lpLockedPct"]))
    for addr, info in (r.get("knownAccounts") or {}).items():
        if isinstance(info, dict) and info.get("type") in ("AMM", "LOCKER"):
            pool_accounts.add(addr)

    holders = r.get("topHolders")
    top10 = None
    creator_pct = None
    creator = r.get("creator")
    if isinstance(holders, list):
        real = [h for h in holders
                if h.get("address") not in pool_accounts and h.get("owner") not in pool_accounts]
        top10 = sum(_f(h.get("pct")) or 0 for h in real[:10])
        if creator:
            creator_pct = sum(_f(h.get("pct")) or 0 for h in real if h.get("owner") == creator)

    risks = [x for x in r.get("risks") or [] if isinstance(x, dict)]
    return {
        "mint_revoked": r.get("mintAuthority") in (None, ""),
        "freeze_revoked": r.get("freezeAuthority") in (None, ""),
        "lp_locked_pct": max(lp_locked) if lp_locked else None,
        "top10_pct": top10,
        "creator": creator,
        "creator_pct": creator_pct,
        "danger_risks": [x.get("name") for x in risks if x.get("level") == "danger"],
        "rugged": bool(r.get("rugged")),
    }
