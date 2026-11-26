"""Price history for the majors bot. Free public data, no account or key.

- 1-hour candles: Binance public market data (data-api.binance.vision), from 2021.
- Funding rates (the fee longs/shorts pay each 8h on futures): Binance futures API.
  Some countries block it → the bot uses a typical 0.01%/8h instead and says so in reports.
Polite: one request at a time, 0.25 s apart, 3 retries with backoff.

Run:  py -m desk.majors.data     download / update (first time ~5–10 min), then a short status
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .. import db as _db
from .config import MajorsSettings

KLINES = "https://data-api.binance.vision/api/v3/klines"
FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"
UA = {"User-Agent": "desk-majors/1.0"}
HOUR = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (symbol TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
                                    PRIMARY KEY (symbol, ts));
CREATE TABLE IF NOT EXISTS funding (symbol TEXT, ts INTEGER, rate REAL, PRIMARY KEY (symbol, ts));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: str | Path):
    con = _db.connect(path)
    con.executescript(SCHEMA)
    return con


def ts_of(date: str) -> int:
    return int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def parse_klines(rows: list, now_ms: int) -> list[tuple]:
    """Binance kline rows → (ts, o, h, l, c, v). Only candles that have closed."""
    out = []
    for r in rows:
        if int(r[6]) >= now_ms:          # closeTime in the future = still forming
            continue
        out.append((int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
    return out


def _get(client: httpx.Client, url: str, params: dict):
    delay = 1.0
    for attempt in range(4):
        try:
            r = client.get(url, params=params, headers=UA, timeout=30)
            if r.status_code in (429, 418) or r.status_code >= 500:
                raise httpx.HTTPStatusError("busy", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except Exception:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(delay)
            delay *= 2
    return None


def update(con, ms: MajorsSettings, client: httpx.Client | None = None, now: float | None = None,
           sleep: float = 0.25) -> dict:
    """Fetch every missing closed hourly candle (and funding) for all symbols."""
    client = client or httpx.Client()
    now_ms = int((now or time.time()) * 1000)
    added = {}
    for sym in ms.symbols:
        last = con.execute("SELECT MAX(ts) FROM candles WHERE symbol=?", (sym,)).fetchone()[0]
        start = (last + HOUR) if last else ts_of(ms.start)
        n = 0
        while start * 1000 < now_ms - HOUR * 1000:
            rows = _get(client, KLINES, {"symbol": sym, "interval": "1h", "startTime": start * 1000, "limit": 1000})
            got = parse_klines(rows or [], now_ms)
            if not got:
                break
            con.executemany("INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?)", [(sym, *g) for g in got])
            con.commit()
            n += len(got)
            start = got[-1][0] + HOUR
            time.sleep(sleep)
        added[sym] = n
    update_funding(con, ms, client, now_ms, sleep)
    return added


def update_funding(con, ms: MajorsSettings, client: httpx.Client, now_ms: int, sleep: float) -> None:
    try:
        for sym in ms.symbols:
            last = con.execute("SELECT MAX(ts) FROM funding WHERE symbol=?", (sym,)).fetchone()[0]
            start = (last + 1) if last else ts_of(ms.start)
            while start * 1000 < now_ms:
                rows = _get(client, FUNDING, {"symbol": sym, "startTime": start * 1000, "limit": 1000})
                if not rows:
                    break
                vals = [(sym, int(r["fundingTime"]) // 1000 // HOUR * HOUR, float(r["fundingRate"])) for r in rows]
                con.executemany("INSERT OR IGNORE INTO funding VALUES (?,?,?)", vals)
                con.commit()
                start = vals[-1][1] + 1
                time.sleep(sleep)
                if len(rows) < 1000:
                    break
        con.execute("INSERT OR REPLACE INTO meta VALUES ('funding', 'real')")
    except Exception:  # noqa: BLE001 — blocked in some countries: fall back to the estimate
        if not con.execute("SELECT COUNT(*) FROM funding").fetchone()[0]:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('funding', 'estimated')")
    con.commit()


def load(con, symbols: list[str], start_ts: int | None = None, end_ts: int | None = None):
    """→ (sorted timestamps, {ts: {sym: (o,h,l,c)}}, {sym: {ts: rate}}, funding_is_real)."""
    q, args = "SELECT symbol, ts, o, h, l, c FROM candles WHERE 1=1", []
    if start_ts is not None:
        q, args = q + " AND ts >= ?", args + [start_ts]
    if end_ts is not None:
        q, args = q + " AND ts <= ?", args + [end_ts]
    bars: dict[int, dict] = {}
    for sym, ts, o, h, l, c in con.execute(q + " ORDER BY ts", args):
        if sym in symbols:
            bars.setdefault(ts, {})[sym] = (o, h, l, c)
    funding: dict[str, dict[int, float]] = {s: {} for s in symbols}
    for sym, ts, rate in con.execute("SELECT symbol, ts, rate FROM funding"):
        if sym in funding:
            funding[sym][ts] = rate
    real = (con.execute("SELECT value FROM meta WHERE key='funding'").fetchone() or ["estimated"])[0] == "real"
    return sorted(bars), bars, funding, real


def btc_change(days: list[str], path: str | Path | None = None) -> str | None:
    """BTC % change over a list of UTC days (first day 00:00 → last day 23:00), or None if no data."""
    if not days:
        return None
    p = Path(path or MajorsSettings().db_path)
    if not p.exists():
        return None
    con = connect(p)
    a = con.execute("SELECT o FROM candles WHERE symbol='BTCUSDT' AND ts >= ? ORDER BY ts LIMIT 1",
                    (ts_of(days[0]),)).fetchone()
    b = con.execute("SELECT c FROM candles WHERE symbol='BTCUSDT' AND ts <= ? ORDER BY ts DESC LIMIT 1",
                    (ts_of(days[-1]) + 23 * HOUR,)).fetchone()
    if not a or not b:
        return None
    return f"{(b[0] / a[0] - 1):+.1%}"


def main() -> None:
    ms = MajorsSettings()
    con = connect(ms.db_path)
    print("Downloading hourly prices (first time takes a few minutes)…")
    added = update(con, ms)
    print("New candles: " + ", ".join(f"{s[:-4]} {n}" for s, n in added.items()))
    for sym in ms.symbols[:1]:
        n, a, b = con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM candles WHERE symbol=?", (sym,)).fetchone()
        if n:
            fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:00")  # noqa: E731
            print(f"{sym[:-4]}: {n} hourly candles, {fmt(a)} → {fmt(b)} UTC")
    f = (con.execute("SELECT value FROM meta WHERE key='funding'").fetchone() or ["estimated"])[0]
    print(f"Funding rates: {f}" + (" (0.01% per 8h used)" if f != "real" else ""))


if __name__ == "__main__":
    main()
