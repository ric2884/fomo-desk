"""STEP 4 — live paper trading. Fake money, real prices, same rules as the backtest.

Needs the recorder running at the same time (it reads the recorder's database).
Run:  python -m desk.paper            (forever)
      python -m desk.paper --report   (results + the gate to real money)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import time
from collections import Counter, defaultdict
from datetime import datetime

from . import db, fields
from .config import ROOT, Settings, load_settings
from .ledger import Ledger
from .strategy import Position, Snap, Trade, check_filters, find_entry, interp

log = logging.getLogger("paper")
STALE_SIGNAL_S = 30       # a dip signal older than this (e.g. after a restart) is not traded
NO_DATA_CLOSE_S = 300     # an open position with no price for 5 min is assumed rugged

PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, symbol TEXT,
    entry_ts REAL, exit_ts REAL, cost REAL, proceeds REAL, reason TEXT, legs TEXT);
CREATE TABLE IF NOT EXISTS paper_open (mint TEXT PRIMARY KEY, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class Coin:
    def __init__(self, mint: str, symbol: str, t0: float):
        self.mint, self.symbol, self.t0 = mint, symbol, t0
        self.snaps: list[Snap] = []
        self.rug: dict | None = None
        self.decided = False                         # entry decision made (traded or skipped)
        self.pending_fill: Snap | None = None        # the signal snapshot waiting for its fill


class PaperDesk:
    def __init__(self, s: Settings, notify=None, rng: random.Random | None = None):
        self.s = s
        self.con = db.connect(s.db_path)
        self.con.executescript(PAPER_SCHEMA)
        self.notify = notify or (lambda text: None)
        self.rng = rng or random.Random()
        self.coins: dict[str, Coin] = {}
        self.positions: dict[str, Position] = {}
        self.watermark = 0.0
        self.skips: Counter = Counter()
        self.ledger = Ledger(s)
        self._restore()

    # ---------- persistence ----------
    def kv_get(self, k: str) -> str | None:
        r = self.con.execute("SELECT value FROM kv WHERE key=?", (k,)).fetchone()
        return r[0] if r else None

    def kv_set(self, k: str, v: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (k, v))
        self.con.commit()

    @property
    def paused(self) -> bool:
        return self.kv_get("paused") == "1"

    def _restore(self) -> None:
        events = []
        for r in self.con.execute("SELECT * FROM paper_trades"):
            events += [(r["entry_ts"], 0, "open", r["mint"], r["cost"]),
                       (r["exit_ts"], 1, "close", r["mint"], r["proceeds"])]
        opens = {r["mint"]: Position.from_dict(json.loads(r["state"]), self.s)
                 for r in self.con.execute("SELECT * FROM paper_open")}
        for m, p in opens.items():
            events.append((p.trade.entry_ts, 0, "open", m, p.trade.cost_usd))
        for ts, _, kind, mint, usd in sorted(events):
            if kind == "open":
                self.ledger.on_open(mint, ts, usd)
            else:
                self.ledger.on_close(mint, ts, usd)
        self.positions = opens
        if opens or events:
            log.info("restored %d open positions, balance $%.2f", len(opens), self.ledger.cash)

    def _save_open(self, p: Position) -> None:
        self.con.execute("INSERT OR REPLACE INTO paper_open VALUES (?, ?)", (p.trade.mint, json.dumps(p.to_dict())))
        self.con.commit()

    # ---------- data ----------
    def _load(self, now: float) -> dict[str, list[Snap]]:
        for r in self.con.execute("SELECT mint, symbol, migrated_at FROM coins WHERE migrated_at > ? AND migrated_at <= ?",
                                  (now - self.s.track_window_s, now)):
            if r["mint"] not in self.coins:
                self.coins[r["mint"]] = Coin(r["mint"], r["symbol"] or r["mint"][:6], r["migrated_at"])
        new: dict[str, list[Snap]] = defaultdict(list)
        for r in self.con.execute("SELECT mint, ts, price, liq FROM snapshots WHERE ts > ? AND ts <= ? ORDER BY ts",
                                  (self.watermark, now)):
            self.watermark = max(self.watermark, r["ts"])
            if r["mint"] in self.coins and r["price"] is not None and r["liq"] is not None:
                new[r["mint"]].append(Snap(r["ts"], r["price"], r["liq"]))
        for m in list(self.coins):
            c = self.coins[m]
            if now - c.t0 > self.s.track_window_s and m not in self.positions:
                del self.coins[m]
            elif c.rug is None:
                row = self.con.execute("SELECT raw_json FROM rugcheck WHERE mint=? ORDER BY ts LIMIT 1", (m,)).fetchone()
                if row:
                    c.rug = fields.parse_rugcheck(json.loads(row[0]))
        return new

    # ---------- trading ----------
    def _skip(self, c: Coin, why: str) -> None:
        c.decided = True
        self.skips[why] += 1

    def _on_entry_snapshot(self, c: Coin, x: Snap, now: float) -> list[str]:
        """Called for each new snapshot of a coin we hold no position in."""
        msgs = []
        if c.pending_fill:                                   # waiting for the 5 s execution delay
            due = c.pending_fill.ts + self.s.exec_delay_s
            if x.ts < due:
                return msgs
            fill = interp(c.pending_fill, x, due)
            c.pending_fill = None
            c.decided = True
            if fill.price <= 0 or fill.liq < self.s.rug_liq_usd:
                self.skips["no fill"] += 1
                return msgs
            why = self.ledger.block_reason(fill.ts, self.s.ticket_usd)
            if why:
                self.skips[why] += 1
                return msgs
            p = Position(c.mint, c.symbol, c.t0, fill, self.s)
            self.positions[c.mint] = p
            self.ledger.on_open(c.mint, fill.ts, p.trade.cost_usd)
            self._save_open(p)
            msgs.append(f"PAPER · Bought {c.symbol} · ${p.trade.cost_usd:.2f} @ {fill.price:.8g}\n"
                        f"https://dexscreener.com/solana/{c.mint}")
            msgs += self._step_position(p, x)                 # the fill snapshot also counts
            return msgs
        if c.decided:
            return msgs
        if x.ts - c.t0 > self.s.max_entry_age_s:
            self._skip(c, "no dip entry")
            return msgs
        i = find_entry(c.snaps, c.t0, self.s)
        if i is None:
            return msgs
        if i != len(c.snaps) - 1 or now - x.ts > STALE_SIGNAL_S:
            self._skip(c, "signal missed (desk was offline)")
            return msgs
        why = check_filters(c.rug, x, self.s)
        if not why and self.paused:
            why = "paused"
        if not why:
            why = self.ledger.block_reason(x.ts, self.s.ticket_usd)
        if not why and self.rng.random() < self.s.fail_rate:
            why = "buy failed (simulated)"
        if why:
            self._skip(c, why.split(" ")[0] if why.startswith(("top10", "lp", "liquidity", "creator", "danger")) else why)
            return msgs
        c.pending_fill = x
        return msgs

    def _step_position(self, p: Position, x: Snap) -> list[str]:
        sold = p.update(x)
        if p.closed:
            return self._close(p)
        self._save_open(p)
        if "take profit" in sold:
            return [f"PAPER · {p.trade.symbol}: sold {self.s.tp_sell_frac:.0%} at take profit, trailing the rest"]
        return []

    def _close(self, p: Position) -> list[str]:
        t = p.trade
        self.positions.pop(t.mint, None)
        self.ledger.on_close(t.mint, t.exit_ts, t.proceeds_usd)
        self.con.execute("DELETE FROM paper_open WHERE mint=?", (t.mint,))
        self.con.execute("INSERT INTO paper_trades (mint, symbol, entry_ts, exit_ts, cost, proceeds, reason, legs) "
                         "VALUES (?,?,?,?,?,?,?,?)", (t.mint, t.symbol, t.entry_ts, t.exit_ts, t.cost_usd,
                                                     t.proceeds_usd, t.exit_reason, json.dumps(t.legs)))
        self.con.commit()
        sign = "+" if t.pnl >= 0 else "−"
        msgs = [f"PAPER · Sold {t.symbol} · {sign}${abs(t.pnl):.2f} ({t.pnl_pct:+.0f}%) · {t.exit_reason}\n"
                f"Balance ${self.ledger.equity:.2f}"]
        if self.ledger.killed:
            msgs.append(f"🛑 PAPER kill switch: balance ${self.ledger.equity:.2f}. No more buys.")
        return msgs

    def step(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        msgs: list[str] = []
        new = self._load(now)
        for mint, snaps in new.items():
            c = self.coins[mint]
            for x in snaps:
                c.snaps.append(x)
                if mint in self.positions:
                    msgs += self._step_position(self.positions[mint], x)
                else:
                    msgs += self._on_entry_snapshot(c, x, now)
        for mint, p in list(self.positions.items()):                 # positions whose price feed died
            if now - p.last.ts > NO_DATA_CLOSE_S:
                p.finish("no price data (assumed rug)", Snap(now, 0.0, 0.0))
                msgs += self._close(p)
        for m in msgs:
            self.notify(m)
        return msgs


# ---------- reporting ----------
def load_trades(con) -> list[Trade]:
    return [Trade(r["mint"], r["symbol"], r["entry_ts"], r["exit_ts"], 0.0, r["cost"], r["proceeds"], r["reason"])
            for r in con.execute("SELECT * FROM paper_trades ORDER BY exit_ts")]


def gate(trades: list[Trade], s: Settings, now: float) -> list[tuple[str, bool, str]]:
    from .backtest import metrics
    led = Ledger(s)
    for t in trades:
        led.on_open(t.mint, t.entry_ts, t.cost_usd)
        led.on_close(t.mint, t.exit_ts, t.proceeds_usd)
    m = metrics({"trades": trades, "equity": led.equity_curve}, s)
    days = (now - trades[0].entry_ts) / 86400 if trades else 0.0
    pf = m["profit_factor"]
    return [
        ("≥ 14 days of paper trading", days >= 14, f"{days:.1f} days"),
        ("≥ 150 closed trades", m["trades"] >= 150, f"{m['trades']} trades"),
        ("net profit after costs > $0", m["net"] > 0, f"${m['net']:.2f}"),
        ("profit factor ≥ 1.3", pf >= 1.3, "inf" if pf == float("inf") else f"{pf:.2f}"),
        ("still positive without the 3 best trades", m["net_ex_top3"] > 0, f"${m['net_ex_top3']:.2f}"),
        ("max drawdown ≤ 30%", m["max_dd_pct"] <= 30, f"{m['max_dd_pct']:.1f}%"),
    ]


def report_text(desk: PaperDesk, now: float, since: float | None = None) -> str:
    trades = load_trades(desk.con)
    recent = [t for t in trades if since is None or t.exit_ts >= since]
    wins = sum(1 for t in recent if t.pnl > 0)
    lines = [f"📊 PAPER report{' (last 24h)' if since else ''}",
             f"Balance: ${desk.ledger.equity:.2f} (start ${desk.s.bankroll_usd:.0f})",
             f"Trades: {len(recent)} · wins {wins} · net ${sum(t.pnl for t in recent):+.2f}",
             f"Open positions: {len(desk.positions)}" + (" · PAUSED" if desk.paused else ""),
             "", "Gate to real money:"]
    g = gate(trades, desk.s, now)
    lines += [f"{'✅' if ok else '❌'} {label}: {val}" for label, ok, val in g]
    lines.append("→ GATE PASSED — we can talk about STEP 5." if all(ok for _, ok, _ in g)
                 else "→ Not yet. Keep paper trading.")
    return "\n".join(lines)


def status_text(desk: PaperDesk) -> str:
    lines = [f"Balance ${desk.ledger.equity:.2f} · cash ${desk.ledger.cash:.2f}"
             + (" · PAUSED" if desk.paused else "") + (" · KILLED" if desk.ledger.killed else "")]
    for p in desk.positions.values():
        chg = (p.last.price / p.fill_price - 1) * 100 if p.fill_price else 0
        lines.append(f"• {p.trade.symbol}: {chg:+.0f}% since buy, {(p.last.ts - p.fill_ts) / 60:.0f} min")
    if not desk.positions:
        lines.append("No open positions.")
    return "\n".join(lines)


async def run(s: Settings) -> None:
    from .telegram import Telegram
    desk = PaperDesk(s)
    tg = Telegram(desk.kv_get, desk.kv_set)
    outbox: list[str] = []
    desk.notify = outbox.append
    if not tg.token:
        print("No TELEGRAM_BOT_TOKEN in .env — messages will only be printed here.")
    await tg.send("🟢 PAPER desk started. " + status_text(desk))
    offline_warned = False

    async def commands():
        while True:
            for cmd in await tg.poll():
                if cmd == "/status":
                    await tg.send(status_text(desk))
                elif cmd == "/report":
                    await tg.send(report_text(desk, time.time()))
                elif cmd == "/pause":
                    desk.kv_set("paused", "1")
                    await tg.send("⏸ Paused: no new buys. Open positions are still managed.")
                elif cmd == "/resume":
                    desk.kv_set("paused", "0")
                    await tg.send("▶️ Resumed.")
            if not tg.client:
                await asyncio.sleep(60)

    task = asyncio.create_task(commands())
    try:
        while True:
            now = time.time()
            try:
                desk.step(now)
            except Exception as e:  # noqa: BLE001 — log and keep going; never die silently
                log.exception("step failed: %r", e)
                outbox.append(f"⚠️ Desk error: {e!r}")
            while outbox:
                await tg.send(outbox.pop(0))
            hb = desk.con.execute("SELECT ts FROM heartbeats WHERE name='recorder'").fetchone()
            if not hb or now - hb[0] > 300:
                if not offline_warned:
                    await tg.send("🔴 Desk offline: the recorder is not running. Start it: py -m desk.recorder")
                    offline_warned = True
            elif offline_warned:
                await tg.send("🟢 Recorder is back.")
                offline_warned = False
            local = datetime.now()
            today = local.strftime("%Y-%m-%d")
            if local.hour >= 8 and desk.kv_get("last_summary") != today:
                desk.kv_set("last_summary", today)
                await tg.send(report_text(desk, now, since=now - 86400))
            await asyncio.sleep(5)
    finally:
        task.cancel()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--reset", action="store_true", help="delete all paper trades and start again at $100")
    a = ap.parse_args()
    s = load_settings()
    if a.reset:
        con = db.connect(s.db_path)
        con.executescript(PAPER_SCHEMA + "DELETE FROM paper_trades; DELETE FROM paper_open;"
                          "DELETE FROM kv WHERE key IN ('paused', 'last_summary');")
        print("Paper results cleared. Next start begins at $%.0f." % s.bankroll_usd)
        return
    if a.report:
        print(report_text(PaperDesk(s), time.time()))
        return
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "logs" / "paper.log")])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(s))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
