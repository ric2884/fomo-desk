# fomo-desk

A small, honest test of one memecoin strategy: buy the first dip of freshly graduated pump.fun coins, with hard money limits.
**Goal: find out whether the strategy has a real edge after all costs, before risking real money.** No returns are promised.

| Step | What | Status |
|---|---|---|
| 1 | Recorder: saves graduations + price paths (no account, no money) | ✅ built |
| 2 | Cost model: FOMO fees, slippage, 5 s delay, failed buys | ✅ built |
| 3 | Backtest: train on first 70% of days, test on the rest | ✅ built |
| 4 | Live paper trading + Telegram + the gate to real money | ✅ built |
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

## Paper trading (STEP 4)

Fake $100, real prices, exactly the same rules as the backtest. The **recorder must keep running** in its own tab.

**Telegram (once):** in Telegram, message **@BotFather** → `/newbot` → pick a name → copy the token it gives you.
In the bot folder run `notepad .env`, click **Yes** to create the file, type one line and save:

```
TELEGRAM_BOT_TOKEN=paste-your-token-here
```

**Start it** (in a second tab, same folder):

```bash
py -m desk.paper
```

Then open your new bot in Telegram and send `/start` — you become its owner. Commands: `/status` `/report` `/pause` `/resume`.
You get a message for every buy and sell, and a daily report at 08:00 with the **gate to real money**:
≥ 14 days · ≥ 150 trades · profit after costs · profit factor ≥ 1.3 · still positive without the 3 best trades · max drawdown ≤ 30%.

Other commands: `py -m desk.paper --report` (same report in the terminal) · `py -m desk.paper --reset` (start over at $100).

## Major-crypto bot (PAPER ONLY) — `majors-and-tuning` branch

BTC, ETH, SOL, BNB, XRP, ADA · hourly since 2018 · long and short · 1× leverage (2× max) · futures fees, slippage,
funding, liquidation · volatility stop-loss (chandelier: k × ATR behind the best price; walk-forward picks off/3/6).
Four strategies (trend, breakout, mean reversion, momentum) are tested on years of history: settings re-picked every
6 months from earlier data only, then judged on the months they never saw, against simply holding BTC.

```bash
py -m desk.majors.data
```
```bash
py -m desk.majors.backtest
```
```bash
py -m desk.majors.live
```

**Profit rule (both bots):** target +1% a day (measured, never promised). Day +1% → no new trades, stops tightened.
**Weekly bank:** at each week's end, if the week made ≥ +2%, that profit moves to a *banked* balance the bot never
trades (winning positions are trimmed if the profit isn't in cash). `bank_mode: "daily"` in settings.json restores
the old daily +2% bank.

## Money limits (in code: `desk/config.py`)

$100 bankroll · $5 tickets · max 3 open · max 15 trades/day · stop for the day at −$8 · pause for the week at −$20 · **kill switch at $60**.
The bot will never withdraw, deposit or send money.
