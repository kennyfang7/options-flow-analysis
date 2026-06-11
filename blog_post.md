---
layout: post
title: "Building an Options Flow Analysis Platform (and What I Learned)"
date: 2026-05-24
categories: [projects, finance, python]
tags: [options, trading, ibkr, python, dash, asyncio]
excerpt: "I built a real-time options flow scanner to track institutional activity — here's how it works and what I learned along the way."
---

I built a real-time options flow scanner connected to Interactive Brokers. This is what it does, why I built it, and what I picked up along the way.

---

## Why I Built This

It started in my MFIN program. We were covering institutional trading mechanics and I got curious — when a big player makes a massive options bet, where does that actually show up? Can you see it in real time?

I've been trading since I was 18, mostly using options to hedge long positions (covered calls, mostly boring stuff). But I always wanted a better read on what *institutional* money was doing, not just retail noise. Most tools that give you this cost a fortune or are locked behind Bloomberg terminals I don't have access to.

So I built one.

---

## What It Does

At a high level: it connects to Interactive Brokers' TWS API, streams live options data, and runs a pipeline to detect unusual activity and potential "smart money" signals.

The core pipeline looks like this:

```
Live tick data
  → classify trade type (block, sweep, split, multi-leg)
  → enrich with Greeks (IV, delta, gamma, etc.)
  → detect unusual activity (volume vs OI, premium size)
  → score for smart money signals
  → fire alerts (Discord webhook)
  → visualise in a Dash dashboard
```

The key thing it's looking for: large, aggressive options trades that look like they might be informed — think a massive OTM call sweep a week before earnings, or a block trade with premium in the hundreds of thousands.

---

## How It Works (Technical)

The whole thing is Python, async-first using `ib_insync` which wraps the IBKR socket API.

**Stack:**
- `ib_insync` — TWS/Gateway connection and data streaming
- `pandas` / `numpy` — analysis
- `SQLite` (dev) / `PostgreSQL` (prod) via SQLAlchemy async
- `Dash` / `Plotly` — live dashboard
- `APScheduler` — periodic tasks
- `loguru` — structured logging
- `pydantic` — data models and validation throughout

**Key modules:**
- `FlowClassifier` — labels each tick as a block, sweep, split, or multi-leg trade
- `GreeksEngine` — enriches trades with IV, delta, gamma, theta, vega (uses IBKR's model Greeks first, falls back to a Black-Scholes implementation)
- `UnusualDetector` — flags anomalies: volume vs open interest ratio, premium size, OTM premium
- `SmartMoneyDetector` — scores trades for institutional characteristics using a weighted confidence system
- `AlertRules` + `Notifier` — translates signals into Discord alerts with severity levels
- `SentimentAggregator` — tracks put/call ratios, net premium, delta/gamma exposure per symbol

One thing that was trickier than expected: IBKR has hard rate limits (50 messages/sec, 60 historical data requests per 10 minutes). I built a centralised async rate limiter with a token bucket for real-time calls and a sliding window for historical requests. Easy to miss this and get your connection throttled.

---

## What I Found

Running it on a paper account, one pattern jumped out early: on a few occasions before notable price moves, there were coordinated OTM call sweeps hitting multiple exchanges in rapid succession — the kind of thing that's nearly invisible if you're just watching charts. One instance was a cluster of short-dated OTM calls on a mid-cap ticker about two days before a sharp move up. Whether that's informed trading or coincidence, I can't say for sure, but it was exactly the kind of signal I built this to surface.

---

## What I Learned

**Technical stuff:**
- Async Python at this scale is genuinely fun once it clicks. `asyncio` + `ib_insync` is a solid combo.
- IBKR's data model is quirky. Options contracts need to be "qualified" before you can request data, con_ids matter a lot, and market data subscriptions are very granular.
- Building a proper validation layer early saves massive headaches later. Every field that comes from IBKR can be `None`, inverted, or just wrong.

**Finance stuff:**
- Greeks are more useful as relative signals than absolute ones. The change in IV across strikes tells you more than a single IV number.
- Open interest is lagged — it updates overnight, not real-time. Volume vs OI ratios have to account for this.
- Most "unusual" flow is noise. You need multiple conditions firing together before it means anything.

---

## What's Next

Still a work in progress. A few things on the roadmap:

- Backtesting signals against historical data
- Migrating to PostgreSQL for production use
- Better IV skew surface (currently using a rough proxy)
- WebSocket-based dashboard updates instead of polling

The code is built in a clean modular pipeline so each piece can be improved independently. That was intentional — I knew I'd want to iterate on the analysis layer without touching the data ingestion.

---

If you're into quantitative finance or just want to poke around the IBKR API, it's a surprisingly accessible project to build. The hard part isn't the code — it's understanding what the data actually means.
