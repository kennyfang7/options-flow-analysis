# Data Problem & Solutions

## Why the Data Problem Exists

The project has a fundamental bottleneck: **IBKR's API was designed for order execution, not bulk data consumption.** We're trying to build a real-time analytics platform on top of an API that actively throttles us.

Here's the math. For a single `fetch_chain("SPY")` with default params (6 expiries, ±15% strikes):

| Step | Contracts | Rate limit hit | Wall time |
|---|---|---|---|
| Qualify underlying | 1 call | 1 general token | ~instant |
| Get price | 1 call | 1 general token | ~instant |
| Get chain params | 1 call | 1 general token | ~instant |
| Build contracts | ~600-900 (6 expiries × 50-75 strikes × 2 sides) | — | instant |
| Qualify batches | 600/50 = 12 batches | 12 general tokens + 0.1s sleep between | ~1.3s |
| Fetch market data | 600/45 = 14 batches | 14 general tokens + 0.5s sleep between | ~8.5s |
| Final settlement delay | — | — | 2s |
| **Total** | | | **~12s per ticker** |

That's one ticker. A 20-stock watchlist = **~4 minutes**. A 50-stock watchlist = **~10 minutes**. And that's just chain snapshots — add historical data and you burn through the 55-req/10-min historical pacing window in about 55 seconds, then you're blocked for up to 9 minutes.

### The Three Hard Constraints

1. **48 msg/sec general limit** — the `RateLimiter._TokenBucket` enforces this, but each batch only consumes 1 token, so this isn't actually the bottleneck
2. **55 historical requests / 10 min** — the real killer for `HistoricalFetcher`; 55 symbols and you're done for 10 minutes
3. **Settlement delays** — the 0.1s, 0.5s, and 2s sleeps in `chain_fetcher.py` aren't rate limiting, they're because IBKR needs time to populate the Ticker objects with market data asynchronously; skip them and you get `None` fields

---

## Innovative Solutions

### 1. Local Cache Layer with Stale-While-Revalidate

Cache snapshots in SQLite with a TTL strategy. Most option chain data doesn't change meaningfully in 30-60 seconds.

```python
# Concept: only re-fetch contracts whose data is older than TTL
async def fetch_chain_cached(self, symbol: str, ttl_seconds: float = 30.0):
    cached = self._cache.get(symbol)
    if cached and (time.time() - cached.timestamp) < ttl_seconds:
        return cached  # serve stale, trigger background refresh
    return await self.fetch_chain(symbol)
```

This alone cuts watchlist scan time by 80%+ on subsequent passes.

### 2. Priority-Tiered Fetching

Not all tickers need the same refresh rate. Split the watchlist into tiers:

- **Tier 1** (active alerts, unusual activity detected): full chain every 15s
- **Tier 2** (actively traded, on watchlist): full chain every 60s
- **Tier 3** (monitoring, low priority): chain every 5 min, or just ATM strikes

```python
# Reduced fetch — only ATM ± 5 strikes instead of ±15% range
await fetcher.fetch_chain("MSFT", max_expiries=2, strike_range_pct=0.03)
```

This is already supported by `fetch_chain` params — it just needs an orchestration layer that dynamically adjusts `max_expiries` and `strike_range_pct` per ticker based on activity.

### 3. Delta Fetching (Only What Changed)

Instead of re-fetching the entire chain, track which contracts had activity and only refresh those:

- Use `reqMktData` streaming (`TickStream`) for real-time ticks on active contracts
- Only call `fetch_chain` when discovering **new** contracts (e.g., new expiry listed, strike range shifted)
- For ongoing monitoring, the tick stream gives bid/ask/volume/OI updates without consuming chain-fetch bandwidth

The `TickStream` is already built for this — the issue is the pipeline currently fetches full chains on a schedule rather than using the stream as the primary data source.

### 4. Parallel Multi-Client Connections

IBKR allows multiple API connections with different client IDs. Each connection gets its own rate limits.

```python
# Two clients, two rate limiters, double throughput
client_a = IBKRClient(client_id=1)  # handles tickers A-M
client_b = IBKRClient(client_id=2)  # handles tickers N-Z

# Each gets 48 msg/sec and 55 hist/10min independently
```

`IBKRClient` already accepts `client_id` from settings — just run two instances and shard the watchlist between them. IBKR allows up to 8 simultaneous connections.

### 5. Supplement with Free/Cheap External Data

Use IBKR only for what it does best (real-time ticks, execution) and get bulk data elsewhere:

- **CBOE data** — delayed options chain snapshots (15 min) are free, good for OI/volume baselines
- **Polygon.io** — REST API with generous rate limits for options snapshots (~$200/mo)
- **Unusual Whales / OptionStrat APIs** — pre-computed unusual activity feeds
- **Finnhub** (already integrated in the project) — good for earnings, basic options data

This lets you build ADV/OI baselines and historical context from external sources, and reserve IBKR bandwidth exclusively for real-time ticks on high-priority contracts.

### 6. Pre-compute and Store Baselines

`HistoricalFetcher` burns 1 historical slot per symbol per call. But ADV (average daily volume) doesn't change much day-to-day. Fetch it once daily at market open and store it:

```python
# Run once at 9:25 AM ET, store results in DB
for symbol in watchlist:
    bars = await fetcher.fetch_bars(symbol, duration="30 D", bar_size="1 day")
    store_adv(symbol, bars.avg_daily_volume())
    # 55 symbols = exactly one window budget, done before market opens
```

Then the real-time pipeline never needs to call `fetch_bars` — it just reads the pre-computed ADV from SQLite.

---

## Recommended Implementation Order

**Solution 6 + Solution 3** gives the most impact for the least code change. Pre-compute baselines once per day, then rely on `TickStream` for real-time flow instead of repeatedly calling `fetch_chain`. This turns the bottleneck from "fetch everything every N seconds" into "fetch once, stream changes."
