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
- Two tabs must keep running on the laptop: recorder (`py -m desk.recorder`) and paper (`py -m desk.paper`).
- Never ask for or display secrets (Telegram token lives only in the laptop's `.env`).
- Strategy rules live in one place (`Position` in `strategy.py`, money limits in `ledger.py`), shared by backtest and paper.
