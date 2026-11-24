# API examples

Short example responses from the three data sources. **Use these when editing `desk/fields.py`** —
it is the only file that reads API responses.

Verified = seen on real data from the desk laptop. Expected = from the code/docs, not yet compared with a real sample.
Real raw samples are saved on the laptop in `logs\raw\<source>\` (first 20 of each). To update this file,
send one of those files (they contain no secrets).

## PumpPortal — `wss://pumpportal.fun/api/data`, `{"method": "subscribeMigration"}`  (expected)

```json
{"signature": "5h…", "mint": "ADvoV8SJXsox5yXdg5ntUP6qRSeDWqtxwg9sbSYEpump", "txType": "migrate", "pool": "pump-amm"}
```
First message after subscribing is `{"message": "Successfully subscribed…"}` — ignored (no `mint`).
Verified indirectly: 1,900+ coins recorded, so `mint` is present.

## DexScreener — `GET /tokens/v1/solana/{mint1,mint2,…}` (up to 30 mints)  (expected shape, verified working)

```json
[{"chainId": "solana", "dexId": "pumpswap", "pairAddress": "…",
  "baseToken": {"address": "<mint>", "name": "Rockstar", "symbol": "ROCKSTAR"},
  "priceUsd": "0.00000209", "liquidity": {"usd": 2031}, "marketCap": 2090, "fdv": 2090,
  "volume": {"m5": 0}, "txns": {"m5": {"buys": 0, "sells": 0}}}]
```
A list, one entry per pool. We pick `dexId == "pumpswap"`, else the deepest pool. `priceUsd` is a string.
Verified: prices/liquidity/market cap printed correctly by `--status`.

## RugCheck — `GET /v1/tokens/{mint}/report`  (verified with `--rugshape`, Nov 20)

```json
{"mintAuthority": null, "freezeAuthority": null, "creator": "HxSXYhTsg4kuiQtp1rJAXHfEbQbiP…",
 "rugged": false, "totalHolders": 25,
 "markets": [{"pubkey": "…", "mintLP": "7Y6NeEr4Kptz2FUd1jkyMC1pqCSkP…",
              "liquidityA": "7s2RWAC9aKPaMHJb61deJLUUPC7aV…", "liquidityB": "6XTpcnjeRrRgxdvYSnUupnG8wvqyZ…",
              "liquidityAAccount": {"mint": "…", "owner": "…", "amount": 0},
              "lp": {"baseMint": "…", "quoteMint": "…", "lpLockedPct": 100}}],
 "topHolders": [{"address": "7s2RWAC9aKPaMHJb61deJLUUPC7aV…", "owner": "6AdGK6BTFev4m5JWm7h4j5pg7LXCU…",
                 "pct": 76.63, "amount": 1532687846200434, "decimals": 6, "insider": false}],
 "risks": [{"name": "Creator history of rugged tokens", "value": "", "description": "…",
            "score": 74400, "level": "danger"}]}
```
Extra fields read by the 8-layer audit (`desk/audit.py`, audit-upgrade branch):
- `topHolders[].insider` (bool) — verified present.
- `markets[].liquidityAAccount.delegate` — verified present (null when no delegate).
- `graphInsidersDetected` (int), `insiderNetworks` ([{"size": n, …}]) — verified present indirectly (Nov 24: the linked-wallets layer failed 22 coins on real data, which needs these fields). Missing → layer "unknown".

Gotchas (learned the hard way):
- `liquidityA/B` are **address strings**, but `liquidityAAccount/BAccount` are **objects** — the first `--check` crashed on this.
- The biggest "holder" is usually the **pool vault** (= `liquidityA`). It must be excluded from top-10 or every coin looks like a whale owns 76%.
- `lp.lpLockedPct` is present (verified: `--check` shows 100).
- `risks[].level == "danger"` catches creator rug history and low liquidity.
