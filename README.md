# fomo-desk

A small, honest test of one memecoin strategy: buy the first dip of freshly graduated pump.fun coins, with hard money limits.
**Goal: find out whether the strategy has a real edge after all costs, before risking real money.** No returns are promised.

| Step | What | Status |
|---|---|---|
| 1 | Recorder: saves graduations + price paths (no account, no money) | ✅ built |
| 2 | Cost model: FOMO fees, slippage, 5 s delay, failed buys | ✅ built |
| 3 | Backtest: train on first 70% of days, test on the rest | ✅ built |
| 4 | Live paper trading + Telegram + the gate to real money | ⏳ after ≥ 7 days of data |
| 5–6 | FOMO connection (read-only first), then live with $5 tickets | ⏳ only if the gate passes |

## Run it on your computer

You need **Python 3.12 or newer** ([python.org/downloads](https://www.python.org/downloads/), on Windows tick "Add Python to PATH").

**1. Download and set up (once)**

```bash
git clone https://github.com/YOUR_USERNAME/fomo-desk.git
cd fomo-desk
python -m venv .venv
```

Windows: `.venv\Scripts\activate` · Mac/Linux: `source .venv/bin/activate`

```bash
pip install -r requirements.txt
```

**2. Test run (1 hour)**

```bash
python -m desk.recorder --minutes 60
```

At the end it prints how many coins it recorded and one coin's price path. Send me that output.

**3. Leave it running for at least 7 days**

```bash
python -m desk.recorder
```

Keep the computer awake (no sleep). If it stops, just start it again; it picks up where it left off.
Check progress any time from a second terminal: `python -m desk.recorder --status`

**4. Backtest**

```bash
python -m desk.backtest --tune
```

Writes `reports/report.md` and `reports/equity.svg`. Send me the report.

## Money limits (in code: `desk/config.py`)

$100 bankroll · $5 tickets · max 3 open · max 15 trades/day · stop for the day at −$8 · pause for the week at −$20 · **kill switch at $60**.
The bot will never withdraw, deposit or send money.
