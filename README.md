# Automated Futures Trading Bot

A Python trading bot for E-mini S&P 500 futures, built against the ProjectX /
TopstepX REST API. Runs unattended across configurable daily sessions, places
exchange-side bracket orders, and enforces its own per-session risk budget.

---

## Strategy

**Opening-range trend scalp.** For each trading session:

1. Build an opening range from the session's first 30 minutes (high, low).
2. Set the session's directional bias from where price closed relative to the
   range midpoint — above the midpoint trades long only, below trades short only.
3. Enter on a pullback to the 9-period EMA that holds: for a long, the bar's low
   touches the EMA, the close recovers above it, the bar is green, and price is
   above session VWAP. Shorts mirror this.
4. Exit via the bracket — 15-tick take profit, 30-tick stop — or a forced flatten
   at session end.

Indicators (EMA, VWAP, opening range) are computed in real time from 1-minute
bars pulled on a 30-second poll.

## Files

| File | Purpose |
|---|---|
| `bot.py` | The bot. API client, indicators, session state machine, risk limits. |
| `env.example` | Template for credentials — copy to `.env` and fill in. |

## Setup

```bash
pip install -r requirements.txt
cp env.example .env         # add your credentials
python bot.py
```

Credentials are read from `.env` via `python-dotenv` and are never committed.

---

## Engineering notes

**Native bracket orders.** Take-profit and stop-loss are attached to the entry
order atomically and held server-side, so a naked position never exists — even if
the process dies mid-trade. Bracket offsets must be signed by direction, which
the API enforces (`Ticks should be less than zero when longing`).

**P&L attribution is bounded by entry time.** An earlier version summed all
realized P&L in a rolling 60-minute window to classify a trade. When a losing
trade fell inside that window, a subsequent *winning* trade was recorded as a
loss — which then tripped the daily loss limit and halted trading for the day.
Results are now queried from the position's own entry timestamp forward.

**Unresolved trades end the session.** If a position closes but the API has not
yet reported realized P&L, the bot retries for up to five minutes. If it still
cannot resolve, the session P&L tracker is knowingly incomplete, so the bot stops
rather than continue against a number it knows is wrong.

**Per-session risk budgets.** Maximum trades and maximum losses are tracked per
day across all sessions, not per session, so adding a second session cannot
silently double daily risk exposure.

**UTF-8 output.** Log output is forced to UTF-8; Windows terminals default to
cp1252 and raise `UnicodeEncodeError` on non-ASCII characters.

---

## Strategy validation

The bot is the execution layer. The more useful work was determining whether the
strategy it runs actually has an edge — it does not, and establishing that was
the point.

| | Backtest | Live |
|---|---|---|
| Win rate | 69% | 56% (25 trades) |

Rather than guess at the cause, the variable was isolated with an out-of-sample
test: identical parameters on a window never used for tuning. The win rate came
back at 60%, splitting the gap between parameter overfitting and unmodeled
execution slippage.

The overfitting was structural rather than careless. Selecting the best of 50+
tested configurations carries roughly a 92% chance of at least one false positive
at p<0.05. This was later confirmed directly: a parameter that looked clearly
optimal across six months showed no measurable effect across 2.5 years.

Three findings that shaped everything after:

1. **Transaction costs dominate at this timeframe.** Commission plus bid-ask
   spread runs roughly $23 per round trip on ES. A later strategy showed a
   statistically real directional signal — z = 7.35 against the random-entry
   baseline — and still lost money, because its gross edge was about $6.86 per
   trade.
2. **Capping winners can destroy a convex edge.** Simulated on the same hit rate:
   an uncapped trailing exit yields +0.29R per trade; capped at 2R it yields
   −0.03R.
3. **Limit entries don't solve slippage on momentum signals.** Adverse selection
   means the strongest breakouts never return to fill the limit, so the saved
   spread is wiped out if more than ~3% of winners are missed.

Account-outcome modeling (Monte Carlo and permutation resampling against
evaluation-account rules) was built separately and is not included here.

---

## Scope

A personal research project. Not investment advice, and not a profitable system —
see above. The engineering and the validation methodology are what's on display.
