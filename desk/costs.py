"""STEP 2 — what every simulated trade pays. Run `python -m desk.costs` for the proof table."""
from __future__ import annotations

from .config import Settings, load_settings


def fomo_fee(usd: float) -> float:
    """FOMO fee for one side of a trade. VERIFY against a real FOMO quote before going live."""
    if usd < 5:
        return 0.10
    if usd <= 47.50:
        return usd * 0.02
    if usd <= 190:
        return 0.95
    return usd * 0.005


def slippage(ticket: float, liq: float, side: str, secs_since_grad: float, s: Settings) -> float:
    if not liq or liq <= 0:
        return 0.99
    slip = max(s.slip_min, ticket / liq * s.slip_impact_mult)
    if side == "buy" and secs_since_grad < s.slip_early_window_s:
        slip = max(slip, s.slip_early_floor)
    return min(slip, 0.99)


def round_trip_pct(ticket: float, liq: float, secs_since_grad: float, s: Settings) -> float:
    """Loss in % if the price doesn't move at all between buy and sell."""
    buy_slip = slippage(ticket, liq, "buy", secs_since_grad, s)
    tokens_value = (ticket - fomo_fee(ticket)) * (1 - buy_slip)
    sell_slip = slippage(tokens_value, liq, "sell", secs_since_grad + 600, s)
    gross = tokens_value * (1 - sell_slip)
    proceeds = gross - fomo_fee(gross)
    return (ticket - proceeds) / ticket * 100


def main() -> None:
    s = load_settings()
    print("Round-trip cost if price is flat (buy at 3 min after graduation, sell 10 min later)\n")
    pools = [15_000, 40_000, 100_000]
    print("ticket  " + "  ".join(f"pool ${p/1000:>4.0f}k" for p in pools))
    for t in (5, 8, 10):
        row = "  ".join(f"{round_trip_pct(t, p, 180, s):>9.2f}%" for p in pools)
        print(f"${t:<6} {row}")
    print("\nSame, buying 10 min after graduation (no early-slippage floor)\n")
    for t in (5, 8, 10):
        row = "  ".join(f"{round_trip_pct(t, p, 600, s):>9.2f}%" for p in pools)
        print(f"${t:<6} {row}")


if __name__ == "__main__":
    main()
