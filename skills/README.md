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

## audit-upgrade branch (not on main yet)

| Skill | Command | Output | When |
|---|---|---|---|
| **Before vs after** | `py -m desk.compare --db C:\fomo-desk-main\fomo-desk-main\data\desk.db` | ≤ 20 lines: old rules vs old + upgrade on all recorded coins, same $100 limits | to decide whether to merge the upgrade |
| Missed check | `py -m desk.missed` | ≤ 20 lines: skipped coins rechecked 6h later, by skip reason | only once the upgraded desk has run live |

Thresholds live in `desk/upgrade_settings.py`; override them in `upgrade.json`.
