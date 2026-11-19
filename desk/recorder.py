"""STEP 1 — the recorder. Watches pump.fun graduations and saves price paths. No account, no money.

Run:   python -m desk.recorder              (forever)
       python -m desk.recorder --minutes 60 (test run)
       python -m desk.recorder --status     (what's in the db)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

import httpx
import websockets

from . import db, fields
from .config import ROOT, Settings, load_settings

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
DEX_URL = "https://api.dexscreener.com/tokens/v1/solana/{}"
RUG_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report"
DEX_BATCH = 30
UA = {"User-Agent": "desk-recorder/1.0"}

log = logging.getLogger("recorder")


class Recorder:
    def __init__(self, s: Settings):
        self.s = s
        self.con = db.connect(s.db_path)
        self.active: dict[str, float] = {}      # mint -> migrated_at
        self.last_poll: dict[str, float] = {}
        self.raw_count: dict[str, int] = {}
        self.dex_gap = 60 / s.dex_max_per_min
        self.dex_next = 0.0
        self.backoff_until = 0.0
        now = time.time()
        for row in self.con.execute("SELECT mint, migrated_at FROM coins WHERE migrated_at > ?",
                                    (now - s.track_window_s,)):
            self.active[row["mint"]] = row["migrated_at"]
        if self.active:
            log.info("resumed tracking %d coins", len(self.active))

    # ---------- helpers ----------
    def save_raw(self, source: str, key: str, payload) -> None:
        n = self.raw_count.get(source, 0)
        if n >= self.s.raw_samples:
            return
        self.raw_count[source] = n + 1
        d = ROOT / "logs" / "raw" / source
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{key[:40]}.json").write_text(json.dumps(payload, indent=1))

    def heartbeat(self, name: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO heartbeats VALUES (?, ?)", (name, time.time()))
        self.con.commit()

    # ---------- websocket ----------
    def on_migration(self, msg: dict) -> None:
        m = fields.parse_migration(msg)
        if not m or m["mint"] in self.active:
            return
        now = time.time()
        self.con.execute("INSERT OR IGNORE INTO coins (mint, pool, migrated_at, signature) VALUES (?,?,?,?)",
                         (m["mint"], m["pool"], now, m["signature"]))
        self.con.commit()
        self.active[m["mint"]] = now
        self.save_raw("pumpportal", m["mint"], msg)
        log.info("graduated %s", m["mint"])

    async def ws_loop(self) -> None:
        delay = 1
        while True:
            try:
                async with websockets.connect(PUMPPORTAL_WS, ping_interval=20) as ws:
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    log.info("websocket connected")
                    delay = 1
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.s.ws_silence_s)
                        try:
                            self.on_migration(json.loads(raw))
                        except json.JSONDecodeError:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — reconnect on anything
                log.warning("websocket: %r — reconnecting in %ss", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    # ---------- DexScreener ----------
    def due_mints(self, now: float) -> list[str]:
        due = []
        for mint, t0 in list(self.active.items()):
            age = now - t0
            if age >= self.s.track_window_s:
                del self.active[mint]
                self.last_poll.pop(mint, None)
                continue
            gap = self.s.fast_poll_s if age < self.s.fast_window_s else self.s.slow_poll_s
            if now - self.last_poll.get(mint, 0) >= gap - 1:
                due.append(mint)
        return due

    async def dex_get(self, client: httpx.AsyncClient, mints: list[str]) -> list[dict] | None:
        wait = max(self.dex_next, self.backoff_until) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        self.dex_next = time.time() + self.dex_gap
        r = await client.get(DEX_URL.format(",".join(mints)))
        if r.status_code == 429:
            self.dex_gap = min(self.dex_gap * 2, 20)
            self.backoff_until = time.time() + 30
            log.warning("DexScreener 429 — slowing down to one call per %.1fs", self.dex_gap)
            return None
        r.raise_for_status()
        self.dex_gap = max(60 / self.s.dex_max_per_min, self.dex_gap * 0.9)
        data = r.json()
        return data if isinstance(data, list) else data.get("pairs") or []

    def store_snapshots(self, mints: list[str], pairs: list[dict], now: float) -> int:
        n = 0
        for mint in mints:
            self.last_poll[mint] = now
            pair = fields.pick_pair(pairs, mint)
            if pair is None:
                continue
            p = fields.parse_pair(pair)
            self.con.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?,?,?)",
                             (mint, now, p["price"], p["liq"], p["mcap"], p["vol_m5"],
                              p["buys_m5"], p["sells_m5"]))
            self.con.execute("UPDATE coins SET name=COALESCE(name, ?), symbol=COALESCE(symbol, ?) WHERE mint=?",
                             (p["name"], p["symbol"], mint))
            n += 1
        self.con.commit()
        return n

    async def poll_loop(self) -> None:
        async with httpx.AsyncClient(timeout=15, headers=UA) as client:
            while True:
                now = time.time()
                due = self.due_mints(now)
                for i in range(0, len(due), DEX_BATCH):
                    batch = due[i:i + DEX_BATCH]
                    try:
                        pairs = await self.dex_get(client, batch)
                    except Exception as e:  # noqa: BLE001
                        log.warning("DexScreener: %r", e)
                        continue
                    if pairs is None:
                        continue
                    self.save_raw("dexscreener", batch[0], pairs)
                    self.store_snapshots(batch, pairs, time.time())
                self.heartbeat("recorder")
                await asyncio.sleep(max(1.0, self.s.fast_poll_s / 3))

    # ---------- RugCheck ----------
    async def rug_loop(self) -> None:
        tried: dict[str, int] = {}
        async with httpx.AsyncClient(timeout=20, headers=UA) as client:
            while True:
                now = time.time()
                have = {r[0] for r in self.con.execute("SELECT DISTINCT mint FROM rugcheck")}
                for mint, t0 in list(self.active.items()):
                    if mint in have or now - t0 < self.s.rugcheck_delay_s or tried.get(mint, 0) >= 3:
                        continue
                    tried[mint] = tried.get(mint, 0) + 1
                    try:
                        r = await client.get(RUG_URL.format(mint))
                        if r.status_code == 429:
                            await asyncio.sleep(30)
                            continue
                        r.raise_for_status()
                        rep = r.json()
                    except Exception as e:  # noqa: BLE001
                        log.warning("RugCheck %s: %r", mint, e)
                        continue
                    self.save_raw("rugcheck", mint, rep)
                    self.con.execute("INSERT OR IGNORE INTO rugcheck VALUES (?,?,?)",
                                     (mint, time.time(), json.dumps(rep)))
                    self.con.commit()
                    await asyncio.sleep(1.5)
                await asyncio.sleep(5)

    async def run(self, minutes: float | None = None) -> None:
        tasks = [asyncio.create_task(c) for c in (self.ws_loop(), self.poll_loop(), self.rug_loop())]
        try:
            if minutes:
                await asyncio.sleep(minutes * 60)
            else:
                await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def status(s: Settings) -> None:
    con = db.connect(s.db_path)
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    print(f"coins recorded:      {q('SELECT COUNT(*) FROM coins')}")
    print(f"price snapshots:     {q('SELECT COUNT(*) FROM snapshots')}")
    print(f"rugcheck reports:    {q('SELECT COUNT(*) FROM rugcheck')}")
    first, last = con.execute("SELECT MIN(migrated_at), MAX(migrated_at) FROM coins").fetchone()
    if first:
        print(f"days covered:        {(last - first) / 86400:.1f}")
    hb = con.execute("SELECT ts FROM heartbeats WHERE name='recorder'").fetchone()
    if hb:
        print(f"last heartbeat:      {time.time() - hb[0]:.0f}s ago")
    row = con.execute("""SELECT c.mint, c.symbol, COUNT(s.ts) n FROM coins c JOIN snapshots s USING(mint)
                         GROUP BY c.mint ORDER BY n DESC LIMIT 1""").fetchone()
    if row:
        print(f"\nexample path: {row['symbol']} ({row['mint']}), {row['n']} snapshots")
        t0 = con.execute("SELECT migrated_at FROM coins WHERE mint=?", (row["mint"],)).fetchone()[0]
        print(f"{'min':>6} {'price':>12} {'liq $':>10} {'mcap $':>10}")
        for r in con.execute("SELECT * FROM snapshots WHERE mint=? ORDER BY ts", (row["mint"],)).fetchall()[::8]:
            print(f"{(r['ts'] - t0) / 60:>6.1f} {r['price'] or 0:>12.8f} {r['liq'] or 0:>10.0f} {r['mcap'] or 0:>10.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float)
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    s = load_settings()
    if a.status:
        return status(s)
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(ROOT / "logs" / "recorder.log")])
    try:
        asyncio.run(Recorder(s).run(a.minutes))
    except KeyboardInterrupt:
        pass
    status(s)


if __name__ == "__main__":
    main()
