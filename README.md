# pm-mm-setup — Polymarket liquidity-rewards market making

A small Python toolkit for providing liquidity on
[Polymarket](https://polymarket.com) markets that have an active LP-rewards
program, and capturing your share of the daily USDC pool. Sister project to
[`mm-setup`](https://github.com/tvdyb/mm-setup) (Kalshi); the architecture
mirrors it, but the rewards math is fundamentally different — see below.

The core piece is `polymarket_rewards_app.py` — a single-file local web app
that surfaces every rewards-active market, your reward score on each side,
your share of the daily pool, and a configurable auto-pennying bot that
keeps a min-size two-sided quote inside the rewards window. The other
scripts are smaller standalone utilities for one-shot placement,
reward-eligibility enforcement, and theo refreshing.

> **Strategy in one line:** for each rewarded market, post `min_size` shares
> of YES bid and `min_size` shares of NO bid both sitting just inside
> `max_spread` cents of the midpoint, do nothing else, repeat.

---

## Why this exists — the Polymarket rewards model

Polymarket pays a per-market daily USDC pool (`rewardsDailyRate`) to makers
who keep size resting close to the midpoint. The formula, sampled once per
minute and integrated over the day, is documented at
<https://docs.polymarket.com/market-makers/liquidity-rewards>:

For each resting BUY at price `p` (probability) with size `b`:

```
s_cents  = max(0, (mid - p) * 100)              # cents below adjusted mid
eligible = (b ≥ rewardsMinSize) AND (s_cents ≤ rewardsMaxSpread)
score    = ((rewardsMaxSpread − s_cents) / rewardsMaxSpread)² × b   if eligible else 0
```

Sum over each side: `Q_yes`, `Q_no`. Per-sample maker score:

```
if 0.10 ≤ mid_yes ≤ 0.90:
    Q = max( min(Q_yes, Q_no),  max(Q_yes, Q_no) / 3 )
else:
    Q = min(Q_yes, Q_no)        # two-sided required at the cliffs
```

Daily payout = `rewardsDailyRate × your_Q_sum / total_Q_sum` (paid in USDC
to the funder address ~midnight UTC; $1 minimum).

Two practical implications:

1. **Two-sided is mandatory in [0.10, 0.90]** for full credit. Single-sided
   liquidity earns at most 1/3 of the score it would earn paired. Outside
   that range, single-sided earns zero.
2. **Closer-to-mid is quadratically better.** A bid 1¢ from mid scores
   `((v−1)/v)²`; a bid 4¢ from mid in a `v=5¢` market scores `(1/5)² = 0.04`
   of the same size — 25× less. So the canonical maker quote is **right at
   the edge of the rewards window** (e.g. 4.5¢ from mid in a 5¢ market) so
   that a 1¢ wiggle in mid doesn't bounce you out.

The risks are also different from Kalshi:

1. **Adverse fills.** A YES bid at 0.96 in a `v=5¢` window that gets hit
   means you took a 96% trade against someone with information. Don't size
   above what you'd be willing to hold to resolution.
2. **Mid drift bouncing you out.** If `mid` moves by more than your distance
   from it, your order goes ineligible (score = 0) for the next sample. The
   reward monitor cancels and re-prices in this case.
3. **Cliffs at 0.10 / 0.90.** When YES mid crosses 0.90, the formula stops
   crediting single-sided liquidity entirely. If a market is sliding to a
   binary outcome, you can wake up to several hours of zero rewards because
   one side ran out and the formula now returns `min(Q_yes, Q_no) = 0`.

---

## Components

### `polymarket_rewards_app.py` — the main app

Single-file `http.server`-based web app. Run it and open
`http://localhost:5050/`.

What it shows per market:

| Column | Meaning |
| ------ | ------- |
| Market | Linked slug + question; `NEG-RISK` and `BLOCKED` badges |
| $/day pool | Market's `rewardsDailyRate` (USDC/day) |
| v¢ | Market's `rewardsMaxSpread` (cents from mid) |
| min | Market's `rewardsMinSize` (shares) |
| YES mid | Adjusted midpoint of the YES book; sub-line shows raw best bid / ask |
| NO mid | Same for NO book |
| my YES / NO | Your total resting size on each side, with top resting price |
| my Q | Your per-sample reward score on this market |
| total Q | Estimated total Q across all makers (lower bound from visible book) |
| share | `my_Q / total_Q` (your slice of the daily pool) |
| $/hr, $/d | Estimated reward run-rate from `share × daily_rate` |
| theo YES¢ | Model fair-value YES probability × 100 |
| edge YES | `theo − best_bid` in cents (positive = mispriced cheap to buy) |
| Actions | place ± / cancel / ⛔ block |

Server-side every ~2.5s the app re-snapshots:

- `GET gamma-api.polymarket.com/markets` — every market with an active
  `rewardsDailyRate > 0`, plus any explicitly-followed slugs you added.
- `GET clob.polymarket.com/book?token_id=…` — fetched in parallel under a
  16-way semaphore for both the YES and NO `clobTokenIds[]`.
- `client.get_orders()` — your current resting orders, joined to rows.
- `theos/<slug>.json` — model fair values, surfaced as the **theo / edge** column.

#### Auto-pennying bot

A background `PennyBot` thread wakes every `interval` seconds and decides
per (market × side) whether to place. The decision tree:

```
if blocked: skip
if v ≤ 0 or min_size ≤ 0:  skip                   # no rewards program
if mid is None:            skip                    # one-sided book

target_p = mid − offset/100                        # offset clamped to ≤ 0.6 × v
if target_p ≥ best_ask:    skip                    # would cross / post-only would reject

# Already-placed check
if my_size ≥ min_size and abs(my_top_px − target_p) ≤ tolerance_c/100 and not is_defense:
    skip

# is_defense: my top order has drifted > 0.8 × v cents from mid (or no orders)
cooldown = defend_cooldown_s if is_defense else cooldown_s
if recently placed within cooldown: skip

place_buy(token_id, target_p, min_size, post_only=True, GTC)
```

Key design decisions:

- **`offset = max(0.5, min(place_offset_c, 0.6 × v))`**. The optimal offset
  is right at the edge of the rewards window for max score weight, but a
  small buffer keeps you eligible if `mid` jitters. Hard-capped at `0.6 × v`
  so that on a `v=2¢` market we don't try to bid at `mid − 5¢`.
- **`tolerance_c` instead of pennying.** Polymarket rewards aren't ranked
  by price (unlike Kalshi's top-300); they're scored by distance from mid.
  So we don't penny — we just maintain a price within `tolerance_c` of the
  target. Defaults: `place_offset_c = 2.0¢`, `tolerance_c = 1.0¢`.
- **`post_only=True` always.** Crossing the spread negates rewards
  eligibility for that fill (and you pay taker fees), so an accidental
  cross is a strict loss.
- **Two cooldowns.** `cooldown_s` (default 180s) caps churn from re-placing
  the same passive quote. `defend_cooldown_s` (default 20s) is for when
  your order has drifted out of the eligibility window.
- **Two-sidedness.** The bot decides each side independently but the
  rewards math means both sides matter — sidedness is the user's
  responsibility. The "place ±" action button on each row places both at
  once, which is the recommended start.
- **Per-market block.** ⛔ Block on any row cancels both YES and NO orders
  on that market and persists the slug to `polymarket_blocked_markets.json`.
  Useful when a market has gone bad (clear directional signal arriving) and
  you want to keep the bot running on everything else.

### `polymarket_reward_monitor.py` — standalone eligibility enforcer

Loops over your resting BUY orders and cancels any whose distance from the
adjusted midpoint has drifted outside `rewardsMaxSpread + slack_cents`. Such
orders score zero — better to free the USDC and let the rewards app
re-place at fresh mid. Runs as a separate process (`--once`, `--dry-run`,
or default loop). Pre-dates the app's PennyBot — kept because it works
without the web UI and is good belt-and-braces.

### `polymarket_place_orders.py` — bulk seed

Place YES + NO post-only GTC bids at `mid − offset` on a single slug or
every market in an event:

```bash
# Dry-run: print what would be placed
python3 polymarket_place_orders.py --slug will-trump-fire-x-by-may --offset 2

# Live, every market in an event
python3 polymarket_place_orders.py --event-slug fed-may-rate-decision --offset 2 --live
```

### `theo_refresh.py` — theo file refresher

Skeleton + helpers for producing `theos/<slug>.json` files. Helpers
included: `mirror_kalshi(ticker)`, `mirror_polymarket(slug)`,
`model_normal_threshold(...)`. To wire a market up: write a function that
returns `{theo_yes, method, confidence, source}` and register it in the
`REFRESHERS` dict. The dashboard picks up theos by slug automatically.

### `overnight_watchdog.py` — relauncher

Supervises `polymarket_rewards_app.py` and `polymarket_reward_monitor.py`,
relaunches with backoff on crash, sends a macOS notification on each
restart so you find out in the morning.

---

## Setup

```bash
git clone https://github.com/tvdyb/pm-mm-setup
cd pm-mm-setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Get your Polymarket private key.
#    - If you signed up via email on polymarket.com: Settings → Wallet →
#      "Export private key". Note your proxy address from the same screen
#      (used as POLYMARKET_FUNDER, with POLYMARKET_SIGNATURE_TYPE=1).
#    - If you use an EOA wallet (e.g. MetaMask connected): export the
#      private key from your wallet, set POLYMARKET_FUNDER to the EOA
#      address, and POLYMARKET_SIGNATURE_TYPE=0.
#
# 2. Fund the funder address with USDC on Polygon (rewards are paid here too).

cp .env.example .env
$EDITOR .env
set -a; source .env; set +a   # or use direnv / a tool of your choice

# 3. Run
python3 polymarket_rewards_app.py
# open http://localhost:5050
```

The auto-pennying bot starts disabled. Toggle it from the UI's penny panel
once you've reviewed the markets the dashboard is showing.

### Environment variables

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `POLYMARKET_PRIVATE_KEY` | *(required for live trading)* | L1 Polygon private key (hex) used to sign EIP-712 orders |
| `POLYMARKET_FUNDER` | *(required for live trading)* | USDC-holding wallet address (EOA or proxy) |
| `POLYMARKET_SIGNATURE_TYPE` | `0` | `0` = EOA, `1` = email/Magic proxy, `2` = browser-wallet proxy |
| `POLYMARKET_CLOB_HOST` | `https://clob.polymarket.com` | CLOB API base |
| `POLYMARKET_GAMMA_HOST` | `https://gamma-api.polymarket.com` | Gamma API base |
| `POLYMARKET_FOLLOWED_PATH` | `./polymarket_followed_markets.json` | Persisted explicitly-followed slugs |
| `POLYMARKET_BLOCKED_PATH` | `./polymarket_blocked_markets.json` | Persisted block-list of slugs the bot must skip |
| `POLYMARKET_THEOS_DIR` | `./theos` | Per-slug JSON files with model YES probabilities |
| `POLYMARKET_CREDS_CACHE` | `./polymarket_api_creds.json` | Cache of L2 (apiKey, secret, passphrase) so we don't re-derive each restart |
| `POLYMARKET_MONITOR_LOG` | `./polymarket_reward_monitor.log` | Where the standalone monitor logs |

---

## Operational notes

- **Auth flow.** `py-clob-client.ClobClient(host, key, chain_id=137,
  signature_type, funder)` then `set_api_creds(create_or_derive_api_creds())`.
  The first call signs an EIP-712 message with your L1 key to derive the L2
  HMAC creds (apiKey/secret/passphrase). We cache those to
  `POLYMARKET_CREDS_CACHE` (mode 600) so subsequent runs skip the re-sign.
- **Tick size and min size are per-market.** `client.get_tick_size(token_id)`
  / `client.get_min_order_size(token_id)`. Most markets are tick=`0.01`,
  min=`5`, but longshot / deep markets can be tick=`0.001` or `0.0001`. The
  rewards-eligibility `rewardsMinSize` is a separate, larger threshold (e.g.
  `200`). Place your orders at exactly `rewardsMinSize` to match the bot's
  default behavior.
- **Negative-risk markets** (`negRisk=true`) are multi-outcome events with a
  single shared collateral pool. py-clob-client handles them when
  `PartialCreateOrderOptions(neg_risk=True)` is passed to `create_order`.
  The dashboard shows a `NEG-RISK` badge so you can review before placing.
- **Rate limits.** CLOB enforces request-rate limits per IP/key; the
  dashboard throttles to 16-way parallel orderbook fetches and re-snapshots
  every 2.5s. The bot reads from the cached snapshot rather than re-hitting
  the API on its own — keeping a 30s cycle nowhere near the limit.
- **Total Q is approximated.** Polymarket's CLOB returns aggregated price
  levels, not individual orders. We treat each visible level as one order
  for scoring purposes; this **under-estimates** total Q (because real
  levels are stacks of many orders, only some of which clear `rewardsMinSize`)
  and therefore **over-estimates** your share. The `$/hr` column should be
  read as a soft upper bound. Reality typically settles to 30–80% of the
  display once enough samples have accumulated.
- **No fill / PnL accounting.** The app shows recent fills via
  `client.get_trades()` but does not reconcile to a database — read your
  Polymarket statements / on-chain USDC balance for actual PnL.

## Theos

`theos/<slug>.json` files supply model fair-value YES probabilities. The
app loads them from `POLYMARKET_THEOS_DIR` (default `./theos`), caches by
mtime, and joins on slug. Schema:

```json
{
  "slug": "will-trump-fire-jerome-powell-by-end-of-2026",
  "theo_yes": 0.18,
  "as_of": "2026-04-30T22:01:11Z",
  "method": "Kalshi mirror price (KXTRUMPFIREPOWELL-26 YES bid 0.17 / ask 0.20)",
  "confidence": "high",
  "source": "kalshi-api"
}
```

For each row the dashboard computes:

- **theo YES¢** = `theo_yes * 100`
- **edge YES** = `theo_yes_c − yes_best_bid_c` (positive ⇒ market bidding YES below model fair, profitable to buy)

Theos are intentionally decoupled from the bot — the bot still chases
rewards, the theo column tells you which markets are also priced
favourably so you can lean in (or pull) manually.

## What's not here

- **No directional signal.** Pure rewards harvester. If you have a view on
  the underlying, this isn't the right tool.
- **No paper-trading mode.** The dry-run flags on the standalone scripts
  work; the web app's bot has no simulator. Use `enabled=false` and watch
  the event log to evaluate behavior before flipping it on.
- **No rewards reconciliation.** Daily USDC payouts arrive at the funder
  address; reconcile against on-chain balance, not the app's `$/hr`
  column (which is a soft upper bound — see above).

## License

Personal project, no license. Don't run it against an account you can't
afford to debug.
