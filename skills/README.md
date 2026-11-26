# Analysis skills

Working commands for checking the desk. Run them in a **new** PowerShell tab, in the bot folder
(`cd C:\fomo-desk-main\fomo-desk-main`). None of them stop or change the recorder or paper trading.
The code lives in `desk/` — these notes say when to use what. Paste only their output into chat.

| Skill | Command | Output | When |
|---|---|---|---|
| Recorder health | `py -m desk.recorder --status \| Select-Object -First 6` | 6 lines: coins, snapshots, reports, days, heartbeat | daily |
| Filter check | `py -m desk.recorder --check` | 13 lines: how filters judge the 12 newest coins | after any change to `fields.py` / filters |
| API shape | `py -m desk.recorder --rugshape` | ~40 lines: structure of one RugCheck report | when RugCheck parsing breaks |
| **Backtest summary** | `py -m desk.summary` | ≤ 20 lines: trades, win rate, avg P/L, max drawdown, verdict, 5 best + 5 worst | Nov 26, then weekly |
| Paper summary | `py -m desk.summary --paper` | ≤ 20 lines, same format, paper trades | any time |
| Gate checklist | `py -m desk.paper --report` (or `/report` in Telegram) | ~13 lines: balance + gate to real money | daily |
| Full backtest report | `py -m desk.backtest --tune` | writes `reports\report.md` + `equity.svg` | only if the summary needs digging into |

## majors-and-tuning branch

| Skill | Command | Output | When |
|---|---|---|---|
| Memecoin re-tune | `py -m desk.tune` | ≤ 20 lines: walk-forward result of 108 settings on unseen days + verdict | weekly; `--apply` only if PROFITABLE |
| Majors prices | `py -m desk.majors.data` | 3 lines: candles downloaded, funding real/estimated | once, then the live bot updates itself |
| Majors test | `py -m desk.majors.backtest` | ≤ 20 lines: 6 strategies (incl. new rotation + vol breakout) vs BTC hold on unseen periods, weekly-target line, picks the best | once, then monthly |
| Majors max mode | `py -m desk.majors.backtest --mode max` | same report: 2×, no lock, no bank, picks the highest average week (paper only) | the user's 5%/week attempt |
| Majors live paper | `py -m desk.majors.live` | runs forever (3rd tab); Telegram `/majors` for status + gate | keep running |
