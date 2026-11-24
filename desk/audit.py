"""(1) 8-layer token safety audit, from the RugCheck report the recorder already saves.

Layers 1–4 reuse fields.parse_rugcheck (verified on real data). Layers 6–8 read extra RugCheck fields:
- 7 insiders:     topHolders[].insider            — verified present (Nov 20 --rugshape)
- 8 delegations:  markets[].liquidityXAccount.delegate — verified present; topHolders[].delegate, risks — expected
- 6 linked:       graphInsidersDetected, insiderNetworks — EXPECTED, not yet seen on real data → "unknown" if missing
Each layer returns pass / fail / unknown. Unknown blocks only if audit_unknown_blocks is on.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import fields
from .upgrade_settings import UpgradeSettings

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"

LAYERS = ["authorities", "dev holding", "top10", "lp locked", "pool age", "linked wallets", "insiders", "delegations"]


@dataclass
class Layer:
    name: str
    result: str
    detail: str = ""


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def audit(raw: dict | None, age_s: float, u: UpgradeSettings) -> list[Layer]:
    if not isinstance(raw, dict):
        return [Layer(n, UNKNOWN, "no RugCheck report") for n in LAYERS]
    try:
        f = fields.parse_rugcheck(raw)
    except Exception:  # noqa: BLE001 — malformed report: fields.py can't read it (pre-existing weakness)
        return [Layer(n, UNKNOWN, "malformed RugCheck report") if n != "pool age"
                else Layer(n, FAIL if age_s < u.audit_min_pool_age_s else PASS, f"{age_s / 60:.1f} min")
                for n in LAYERS]
    out: list[Layer] = []

    # 1 mint + freeze authority
    ok = f["mint_revoked"] and f["freeze_revoked"]
    out.append(Layer("authorities", PASS if ok else FAIL,
                     "" if ok else ("mint" if not f["mint_revoked"] else "freeze") + " not revoked"))

    # 2 developer holding
    if f["creator"] is None or f["creator_pct"] is None:
        out.append(Layer("dev holding", UNKNOWN, "creator unknown"))
    else:
        bad = f["creator_pct"] > u.audit_max_dev_pct
        out.append(Layer("dev holding", FAIL if bad else PASS, f"{f['creator_pct']:.1f}%"))

    # 3 top-10 holders (pool excluded)
    if f["top10_pct"] is None:
        out.append(Layer("top10", UNKNOWN, "no holders"))
    else:
        bad = f["top10_pct"] > u.audit_max_top10_pct
        out.append(Layer("top10", FAIL if bad else PASS, f"{f['top10_pct']:.1f}%"))

    # 4 liquidity locked
    if f["lp_locked_pct"] is None:
        out.append(Layer("lp locked", UNKNOWN, "no lp data"))
    else:
        bad = f["lp_locked_pct"] < u.audit_min_lp_locked_pct
        out.append(Layer("lp locked", FAIL if bad else PASS, f"{f['lp_locked_pct']:.0f}%"))

    # 5 pool age at the moment of the buy
    bad = age_s < u.audit_min_pool_age_s
    out.append(Layer("pool age", FAIL if bad else PASS, f"{age_s / 60:.1f} min"))

    # 6 linked wallets (insider graph)
    graph = _num(raw.get("graphInsidersDetected"))
    nets = raw.get("insiderNetworks")
    sizes = [_num(n.get("size")) or 0 for n in nets if isinstance(n, dict)] if isinstance(nets, list) else None
    if graph is None and sizes is None:
        out.append(Layer("linked wallets", UNKNOWN, "no graph data"))
    else:
        biggest = max(sizes) if sizes else 0
        bad = (graph or 0) > u.audit_max_linked_insiders or biggest >= u.audit_max_network_size
        out.append(Layer("linked wallets", FAIL if bad else PASS, f"graph {graph:.0f}, net {biggest:.0f}"
                         if graph is not None else f"net {biggest:.0f}"))

    # 7 insiders (% of supply held by flagged wallets, pool excluded via fields' top10 logic)
    holders = raw.get("topHolders")
    if not isinstance(holders, list) or not holders or not any("insider" in h for h in holders if isinstance(h, dict)):
        out.append(Layer("insiders", UNKNOWN, "no insider flags"))
    else:
        pct = sum(_num(h.get("pct")) or 0 for h in holders if isinstance(h, dict) and h.get("insider"))
        bad = pct > u.audit_max_insider_pct
        out.append(Layer("insiders", FAIL if bad else PASS, f"{pct:.1f}%"))

    # 8 delegations (someone else allowed to move tokens)
    delegated = []
    for m in raw.get("markets") or []:
        for k in ("liquidityAAccount", "liquidityBAccount"):
            acc = m.get(k) if isinstance(m, dict) else None
            if isinstance(acc, dict) and acc.get("delegate"):
                delegated.append(k)
    for h in holders if isinstance(holders, list) else []:
        if isinstance(h, dict) and h.get("delegate"):
            delegated.append("holder")
    for r in raw.get("risks") or []:
        if isinstance(r, dict) and "delegat" in str(r.get("name", "")).lower():
            delegated.append("risk")
    out.append(Layer("delegations", FAIL if delegated else PASS, ", ".join(delegated[:3])))
    return out


def audit_reason(layers: list[Layer], u: UpgradeSettings) -> str | None:
    """First blocking layer as a skip reason, e.g. 'audit: insiders 23.0%'. None = passes."""
    for layer in layers:
        if layer.result == FAIL or (layer.result == UNKNOWN and u.audit_unknown_blocks):
            return f"audit: {layer.name}" + (f" {layer.detail}" if layer.detail else "")
    return None
