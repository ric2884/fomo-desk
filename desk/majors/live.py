"""Majors live PAPER trading. Fake $100, real hourly prices, same engine as the backtest. No exchange, no keys.

Run (third tab, keep open):  py -m desk.majors.live
Needs: py -m desk.majors.data and py -m desk.majors.backtest done once (they pick the strategy).
Telegram: uses the same bot as the memecoin desk (sends only; ask with /majors in the memecoin chat).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import ROOT, Settings, load_settings
from . import data
from .config import MajorsSettings
from .engine import Engine
from .strategies import make

log = logging.getLogger("majors")
HOUR = 3600


def state_path(ms: MajorsSettings) -> Path:
    return Path(ms.db_path).with_name("majors_state.json")


def load_choice(ms: MajorsSettings) -> dict | None:
    p = Path(ms.db_path).with_name("majors_choice.json")
    return json.loads(p.read_text()) if p.exists() else None


class LiveMajors:
    def __init__(self, ms: MajorsSettings, s: Settings, choice: dict, con):
        self.ms, self.s, self.choice, self.con = ms, s, choice, con
        self.engine = Engine(ms, s, self._strats(), self._funding())
        self.engine.atr_k = choice.get("atr_k", 0.0)
        self.meta = {"choice": choice, "started": 0, "btc_start": None}
        p = state_path(ms)
        if p.exists():
            saved = json.loads(p.read_text())
            if saved.get("meta", {}).get("choice") == choice:
                self.engine.restore(saved["engine"])
                self.meta = saved["meta"]
        self._warm()

    def _strats(self):
        c = self.choice
        return {sym: make(c["family"], tuple(c["params"]), c["allow_short"]) for sym in self.ms.symbols}

    def _funding(self):
        out = {s: {} for s in self.ms.symbols}
        for sym, ts, rate in self.con.execute("SELECT symbol, ts, rate FROM funding WHERE ts > ?",
                                              (int(time.time()) - 30 * 86400,)):
            if sym in out:
                out[sym][ts] = rate
        return out

    def _bars_between(self, a: int, b: int) -> list[tuple[int, dict]]:
        rows = self.con.execute("SELECT symbol, ts, o, h, l, c FROM candles WHERE ts > ? AND ts <= ? ORDER BY ts", (a, b))
        out: dict[int, dict] = {}
        for sym, ts, o, h, l, c in rows:
            if sym in self.ms.symbols:
                out.setdefault(ts, {})[sym] = (o, h, l, c)
        return sorted(out.items())

    def _latest_ts(self) -> int:
        return self.con.execute("SELECT MAX(ts) FROM candles").fetchone()[0] or 0

    def _warm(self) -> None:
        """Rebuild indicators from history up to where trading stands (or now, on a fresh start)."""
        fresh = not self.engine.last_ts
        upto = self.engine.last_ts or self._latest_ts()
        for ts, bars in self._bars_between(upto - self.ms.warm_bars * HOUR, upto):
            for sym, b in bars.items():
                self.engine.strats[sym].update(b, 0, 0, ts)
                if fresh:                                 # a restored state already carries its volatility
                    self.engine.observe(sym, b)
        if not self.engine.last_ts:                       # fresh start: trade from the next new bar
            self.engine.last_ts = upto
            self.meta["started"] = upto
            btc = self.con.execute("SELECT c FROM candles WHERE symbol='BTCUSDT' AND ts=?", (upto,)).fetchone()
            self.meta["btc_start"] = btc[0] if btc else None
            self.save()

    def catch_up(self) -> list[str]:
        ev = []
        self.engine.funding = self._funding()            # pick up newly downloaded funding rates
        for ts, bars in self._bars_between(self.engine.last_ts, self._latest_ts()):
            if len(bars) < len(self.ms.symbols) and ts == self._latest_ts():
                break                                     # wait until every coin's candle is in
            for e in self.engine.on_bar(ts, bars):
                ev.append(f"MAJORS · {e}")
        self.save()
        return ev

    def save(self) -> None:
        state_path(self.ms).write_text(json.dumps({"meta": self.meta, "engine": self.engine.state()}))

    def btc_since_start(self) -> float | None:
        b0 = self.meta.get("btc_start")
        b1 = self.engine.last.get("BTCUSDT")
        return (b1 / b0 - 1) if b0 and b1 else None


def status_text(st: dict, s: Settings | None = None) -> str:
    """Short status from a saved state (also used by the memecoin bot's /majors command)."""
    s = s or load_settings()
    e, meta = st["engine"], st["meta"]
    pos_val = sum(p["margin"] + p["side"] * p["qty"] * (e["last"].get(sym, p["entry"]) - p["entry"])
                  for sym, p in e["pos"].items())
    total = e["cash"] + e["banked"] + pos_val
    days = [d for d in e["days"] if d[0] >= datetime.fromtimestamp(meta["started"], timezone.utc).strftime("%Y-%m-%d")]
    hit = sum(1 for d in days if d[3])
    c = meta["choice"]
    stop = f"stop {c.get('atr_k', 0):g}×ATR" if c.get("atr_k") else "no stop-loss"
    lines = [f"📈 MAJORS PAPER · {c['family']} {tuple(c['params'])} {'long+short' if c['allow_short'] else 'long only'}, {stop}",
             f"Total ${total:.2f} (start ${s.bankroll_usd:.0f}) · banked ${e['banked']:.2f}"
             + (" · KILLED" if e["killed"] else "") + (f" · today: {e['day_state']}" if e["day_state"] != "normal" else ""),
             f"Days reaching +1%: {hit} of {len(days)}"]
    b0, b1 = meta.get("btc_start"), e["last"].get("BTCUSDT")
    if b0 and b1:
        lines.append(f"Bot {total / s.bankroll_usd - 1:+.1%} vs BTC hold {b1 / b0 - 1:+.1%} since start")
    for sym, p in e["pos"].items():
        px = e["last"].get(sym, p["entry"])
        lines.append(f"• {'long' if p['side'] > 0 else 'short'} {sym[:-4]} {p['side'] * (px / p['entry'] - 1):+.1%}")
    if not e["pos"]:
        lines.append("No open positions.")
    n_days = (e["last_ts"] - meta["started"]) / 86400
    peak, mdd = s.bankroll_usd, 0.0
    for _, _, end, _ in days:
        peak = max(peak, end)
        mdd = max(mdd, (peak - end) / peak)
    beat = b0 and b1 and total / s.bankroll_usd - 1 > b1 / b0 - 1
    gate = [("≥ 14 days", n_days >= 14), ("profit after costs", total > s.bankroll_usd),
            ("beats BTC hold", bool(beat)), ("max drawdown ≤ 30%", mdd <= 0.30)]
    lines.append("Gate: " + " · ".join(f"{'✅' if ok else '❌'} {g}" for g, ok in gate))
    return "\n".join(lines)


def status_from_file(path: Path | None = None) -> str:
    p = path or state_path(MajorsSettings())
    if not p.exists():
        return "Majors bot not started yet."
    return status_text(json.loads(p.read_text()))


def _telegram():
    """Send-only Telegram using the memecoin desk's bot and owner (no polling, so no conflict)."""
    from ..telegram import Telegram
    desk_db = load_settings().db_path

    def kv_get(k):
        try:
            con = sqlite3.connect(desk_db)
            r = con.execute("SELECT value FROM kv WHERE key=?", (k,)).fetchone()
            con.close()
            return r[0] if r else None
        except sqlite3.Error:
            return None
    return Telegram(kv_get, lambda k, v: None)


async def run() -> None:
    ms, s = MajorsSettings(), load_settings()
    choice = load_choice(ms)
    if not choice:
        print("No strategy chosen yet. Run first: py -m desk.majors.data  then  py -m desk.majors.backtest")
        return
    ms = MajorsSettings(leverage=choice.get("leverage", 1.0))
    con = data.connect(ms.db_path)
    await asyncio.to_thread(data.update, con, ms)
    live = LiveMajors(ms, s, choice, con)
    tg = _telegram()
    await tg.send("🟢 MAJORS paper bot started.\n" + status_from_file(state_path(ms)))
    while True:
        try:
            now = time.time()
            if now >= live.engine.last_ts + 2 * HOUR + 60:          # a new hourly candle has closed
                await asyncio.to_thread(data.update, con, ms)
                for e in live.catch_up():
                    await tg.send(e)
            local = datetime.now()
            if local.hour >= 8 and local.minute >= 10 and live.meta.get("last_report") != local.strftime("%Y-%m-%d"):
                live.meta["last_report"] = local.strftime("%Y-%m-%d")
                live.save()
                await tg.send(status_from_file(state_path(ms)))
        except Exception as e:  # noqa: BLE001 — never die silently
            log.exception("majors step failed")
            await tg.send(f"⚠️ MAJORS error: {e!r}")
        await asyncio.sleep(60)


def main() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "logs" / "majors.log")])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
