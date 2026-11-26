# Working rules for this project

Context: a memecoin paper-trading desk for a beginner on Windows (PowerShell, `py` not `python`).
Goal: find out honestly whether the strategy has an edge after costs before any real money. Never promise returns.

1. **No raw data or long logs in chat.** For any analysis, use or write a script that prints a summary of
   **at most 20 lines**: trades, win rate, avg P/L, max drawdown, 5 best + 5 worst trades.
   Existing: `py -m desk.summary` (backtest) and `py -m desk.summary --paper`. Ask the user for that output, not for files or logs.
2. **API calls stay batched and polite.** DexScreener: up to 30 mints per request, ≤ 250 requests/min, slow down on 429.
   RugCheck: one report per coin, 3 retries, back off on 429. Don't add parallelism unless a real bottleneck shows up,
   and don't make the user restart the recorder mid-collection without a strong reason.
3. **Use `docs/api-examples.md`** when touching `desk/fields.py` (the only place that reads API responses).
   Keep it short and mark each example as verified or expected. Update it when real data shows a new shape.
4. **Working analysis commands are listed in `skills/README.md`** with how to run them. Add new ones there.
   Code stays in `desk/` (one copy), the note says when and how to use it.

Also:
- Every code change: run `python3 -m pytest -q`, commit, push. The user updates by downloading the ZIP and choosing "Replace".
- Tabs that keep running on the laptop: recorder (`py -m desk.recorder`), memecoin paper (`py -m desk.paper`),
  and (majors-and-tuning branch) majors paper (`py -m desk.majors.live`).
- Majors bot is PAPER ONLY, long+short, leverage 1× default and 2× hard max. No real money, no live shorts, no exchange
  connection until a bot passes its 14-day paper gate AND we've checked which futures exchange is legal for the user. Ask first.
- Profit rule: +1%/day is a target to measure against, never a promise. Day +1% → lock new entries. Profit is banked
  WEEKLY (week ≥ +2%), the user's choice; `bank_mode` "daily" is the old rule. Banked money is never traded.
- Majors MAX MODE (`py -m desk.majors.backtest --mode max`, user's choice to chase 5%/week ON PAPER): 2× (hard cap),
  no daily lock, no bank, picks the highest average week. Money limits still apply. It fails the 30% drawdown gate,
  so it can never go to real money under current rules — say so plainly; never raise leverage above 2×.
- Majors stop-loss is a chandelier ATR stop; the walk-forward picks off/3×/6× per strategy from earlier data only.
- Re-tuning is walk-forward only (pick settings on earlier days, test on later ones). Never apply settings that
  weren't profitable on unseen data with ≥ 30 trades.
- Never ask for or display secrets (Telegram token lives only in the laptop's `.env`).
- Strategy rules live in one place (`Position` in `strategy.py`, money limits in `ledger.py`), shared by backtest and paper.
