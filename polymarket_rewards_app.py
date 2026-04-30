"""Polymarket Rewards Tracker — local web app showing the markets you're providing
liquidity on, your share of the LP rewards pool on each, and recent fills.

Reward model (per Polymarket docs, sampled once per minute):

  For each resting BUY on a side at price p (probability), with order size b:
      s_cents = max(0, mid_cents - p_cents)            # distance below mid, in ¢
      eligible = (b >= min_size) and (s_cents <= v)    # v = max_spread (¢)
      score   = ((v - s_cents) / v) ** 2 * b           # 0 if not eligible

  Per side (YES token, NO token), sum scores → Q_yes, Q_no.
  Per-sample market Q for a maker:
      if 0.10 <= mid_yes <= 0.90:
          Q = max( min(Q_yes, Q_no),  max(Q_yes, Q_no) / c )    # c = 3
      else:
          Q = min(Q_yes, Q_no)        # two-sided required at the cliffs

  Daily payout = daily_rate * sum(your_Q over samples) / sum(total_Q over samples).
  Single-sided liquidity in [0.10, 0.90] earns 1/3 of two-sided. Outside that
  range it earns zero.

Usage:
    python3 polymarket_rewards_app.py             # serves on http://localhost:5050
    python3 polymarket_rewards_app.py --port 8080
"""
import argparse, datetime as dt, json, math, os, secrets, threading, time
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse
import requests

CLOB_HOST  = os.environ.get("POLYMARKET_CLOB_HOST",  "https://clob.polymarket.com")
GAMMA_HOST = os.environ.get("POLYMARKET_GAMMA_HOST", "https://gamma-api.polymarket.com")
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
FUNDER      = os.environ.get("POLYMARKET_FUNDER", "")
SIG_TYPE    = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
CHAIN_ID    = 137  # Polygon

FOLLOWED_PATH = Path(os.environ.get("POLYMARKET_FOLLOWED_PATH", "./polymarket_followed_markets.json"))
BLOCKED_PATH  = Path(os.environ.get("POLYMARKET_BLOCKED_PATH",  "./polymarket_blocked_markets.json"))
THEOS_DIR     = Path(os.environ.get("POLYMARKET_THEOS_DIR",     "./theos"))
CREDS_CACHE   = Path(os.environ.get("POLYMARKET_CREDS_CACHE",   "./polymarket_api_creds.json"))

REWARD_C = 3.0          # single-sided penalty multiplier
MID_LO   = 0.10         # below this, two-sided required
MID_HI   = 0.90         # above this, two-sided required
SAMPLES_PER_DAY = 1440  # one per minute
LOCAL_MUTATION_TOKEN = secrets.token_urlsafe(24)

# Polymarket has thousands of low-rate sports / esports rewards programs.
# By default we only orderbook-fetch the top N by daily_rate (plus any
# explicitly followed slugs); raise via --top-n to widen.
DEFAULT_TOP_N = 1700

# Below this daily pool, the market isn't worth orderbook-fetching even if
# it's "uncontested" — the absolute payout is too small to bother. Override
# with --min-pool. Set to 0 to include every active program.
DEFAULT_MIN_POOL_USD = 10.0

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
HDRS = {"User-Agent": UA, "Accept": "application/json"}
HTTP_TIMEOUT = 12


# ---------------------------------------------------------------------------
# CLOB client (lazy init — None until private key + creds are set up).
# ---------------------------------------------------------------------------

_clob_client = None
_clob_lock = threading.Lock()


def get_clob():
    """Return a cached py-clob-client.ClobClient, or None if not configured.
    Only the auto-pennying / cancel paths need this; read-only views work
    against the public CLOB orderbook endpoint without it."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    if not PRIVATE_KEY or not FUNDER:
        return None
    with _clob_lock:
        if _clob_client is not None:
            return _clob_client
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        c = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                       signature_type=SIG_TYPE, funder=FUNDER)
        creds = None
        if CREDS_CACHE.exists():
            try:
                d = json.loads(CREDS_CACHE.read_text())
                creds = ApiCreds(api_key=d["api_key"], api_secret=d["api_secret"],
                                 api_passphrase=d["api_passphrase"])
            except Exception:
                creds = None
        if creds is None:
            creds = c.create_or_derive_api_creds()
            try:
                CREDS_CACHE.write_text(json.dumps({
                    "api_key": creds.api_key,
                    "api_secret": creds.api_secret,
                    "api_passphrase": creds.api_passphrase,
                }))
                CREDS_CACHE.chmod(0o600)
            except Exception:
                pass
        c.set_api_creds(creds)
        _clob_client = c
        return _clob_client


# ---------------------------------------------------------------------------
# Public read APIs (Gamma + CLOB orderbook).
# ---------------------------------------------------------------------------

_progs_cache = {"data": {}, "ts": 0.0}
_meta_cache = {"data": {}, "ts": 0.0}
PROGS_CACHE_S = 60   # CLOB rewards programs change on day boundaries
META_CACHE_S  = 300  # slug/question/tokens are immutable per market


def clob_rewards_programs():
    """Pull every active rewards program from the CLOB. Authoritative source
    of (rewardsMaxSpread, rewardsMinSize, daily_rate) per condition_id —
    Gamma's /markets endpoint doesn't include the daily rate.

    Response shape per entry:
      {
        "condition_id":      "0x…",
        "rewards_max_spread": 4.5,        # cents
        "rewards_min_size":   200,        # shares
        "total_daily_rate":   50,         # USDC/day
        "native_daily_rate":  50,
        "rewards_config": [{...,"rate_per_day": 50,"start_date","end_date"}]
      }
    """
    if time.time() - _progs_cache["ts"] < PROGS_CACHE_S and _progs_cache["data"]:
        return _progs_cache["data"]
    out = {}
    cursor = ""
    for _ in range(50):  # safety bound on pagination
        params = {"limit": 500}
        if cursor: params["next_cursor"] = cursor
        r = requests.get(f"{CLOB_HOST}/rewards/markets/current",
                         params=params, headers=HDRS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        d = r.json() or {}
        for entry in d.get("data") or []:
            cid = (entry.get("condition_id") or "").lower()
            if not cid: continue
            out[cid] = {
                "condition_id": cid,
                "rewards_max_spread_c": _safe_float(entry.get("rewards_max_spread")),
                "rewards_min_size":     _safe_float(entry.get("rewards_min_size")),
                "rewards_daily_rate":   _safe_float(entry.get("total_daily_rate"))
                                        or _safe_float(entry.get("native_daily_rate")),
                "rewards_config":       entry.get("rewards_config") or [],
            }
        cursor = d.get("next_cursor") or ""
        if not cursor or cursor in ("LTE=", "MA=="):  # base64 sentinels seen in CLOB v2
            break
    _progs_cache["data"] = out
    _progs_cache["ts"] = time.time()
    return out


def gamma_markets_by_condition_ids(condition_ids, batch=100):
    """Fetch market metadata for a set of condition_ids. Gamma /markets
    accepts repeated `condition_ids=` so we batch. Caches per condition_id
    for META_CACHE_S since slug/question/tokens are immutable."""
    out = {}
    miss = []
    now = time.time()
    cache_fresh = (now - _meta_cache["ts"]) < META_CACHE_S
    for cid in condition_ids:
        cached = _meta_cache["data"].get(cid)
        if cached and cache_fresh:
            out[cid] = cached
        else:
            miss.append(cid)
    for i in range(0, len(miss), batch):
        chunk = miss[i:i+batch]
        params = [("condition_ids", c) for c in chunk]
        params.append(("limit", str(batch * 2)))
        try:
            r = requests.get(f"{GAMMA_HOST}/markets", params=params,
                             headers=HDRS, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            for m in (r.json() or []):
                cid = (m.get("conditionId") or "").lower()
                if cid:
                    out[cid] = m
                    _meta_cache["data"][cid] = m
        except Exception:
            continue
    _meta_cache["ts"] = now
    return out


def gamma_markets(active=True, closed=False, archived=False, limit=500,
                  rewards_only=True, extra_params=None, top_n=None,
                  followed_slugs=None, min_pool_usd=None):
    """Return a list of normalized market dicts for currently-rewarded
    markets, ranked by daily_rate descending and capped to `top_n` (default
    DEFAULT_TOP_N — Polymarket has 5000+ active programs and orderbook
    fetching all of them takes minutes). Programs with daily_rate below
    `min_pool_usd` (default DEFAULT_MIN_POOL_USD) are dropped first.
    Slugs in `followed_slugs` are kept even if they fall below either cap.
    Pulls program list from CLOB, then enriches with Gamma metadata
    (slug, question, clobTokenIds, negRisk).
    """
    progs = clob_rewards_programs()
    if not progs:
        return []
    floor = DEFAULT_MIN_POOL_USD if min_pool_usd is None else min_pool_usd
    if rewards_only:
        cids = [c for c, p in progs.items()
                if (p.get("rewards_daily_rate") or 0) >= floor]
    else:
        cids = list(progs.keys())
    # Sort programs by rate desc and cap.
    cids.sort(key=lambda c: -(progs[c].get("rewards_daily_rate") or 0))
    cap = top_n if top_n is not None else DEFAULT_TOP_N
    cids_top = cids[:cap]
    meta = gamma_markets_by_condition_ids(cids_top)
    out = []
    for cid in cids_top:
        prog = progs[cid]
        m = meta.get(cid)
        if not m:
            continue
        if active and not m.get("active"): continue
        if not closed and m.get("closed"): continue
        if not archived and m.get("archived"): continue
        norm = _normalize_market(m)
        norm["rewards_max_spread_c"] = prog["rewards_max_spread_c"]
        norm["rewards_min_size"]     = prog["rewards_min_size"]
        norm["rewards_daily_rate"]   = prog["rewards_daily_rate"]
        out.append(norm)
    # Pull in followed-slug markets even if they're below the cap.
    if followed_slugs:
        have = {m["slug"] for m in out}
        for slug in followed_slugs:
            if slug in have: continue
            try:
                m = gamma_market_by_slug(slug)
            except Exception:
                continue
            if not m: continue
            cid = (m.get("condition_id") or "").lower()
            prog = progs.get(cid)
            if prog:
                m["rewards_max_spread_c"] = prog["rewards_max_spread_c"]
                m["rewards_min_size"]     = prog["rewards_min_size"]
                m["rewards_daily_rate"]   = prog["rewards_daily_rate"]
            out.append(m)
    return out


def gamma_market_by_slug(slug):
    r = requests.get(f"{GAMMA_HOST}/markets", params={"slug": slug},
                     headers=HDRS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    arr = r.json() or []
    if not arr:
        return None
    return _normalize_market(arr[0])


def _safe_float(x):
    try:
        if x is None: return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _normalize_market(m):
    """Pull just the fields the dashboard needs out of a Gamma market dict."""
    tokens = m.get("clobTokenIds")
    if isinstance(tokens, str):
        try: tokens = json.loads(tokens)
        except Exception: tokens = []
    tokens = tokens or []
    yes_tok = str(tokens[0]) if len(tokens) > 0 else None
    no_tok  = str(tokens[1]) if len(tokens) > 1 else None
    outcome_prices = m.get("outcomePrices")
    if isinstance(outcome_prices, str):
        try: outcome_prices = json.loads(outcome_prices)
        except Exception: outcome_prices = []
    return {
        "id": str(m.get("id") or ""),
        "condition_id": m.get("conditionId") or "",
        "slug": m.get("slug") or "",
        "question": m.get("question") or "",
        "event_slug": (m.get("events") or [{}])[0].get("slug") if isinstance(m.get("events"), list) else "",
        "yes_token": yes_tok,
        "no_token":  no_tok,
        "neg_risk": bool(m.get("negRisk") or False),
        "outcome_yes_price": _safe_float((outcome_prices or [None, None])[0]),
        "outcome_no_price":  _safe_float((outcome_prices or [None, None])[1]) if len(outcome_prices or []) > 1 else None,
        "rewards_max_spread_c": _safe_float(m.get("rewardsMaxSpread")),
        "rewards_min_size":     _safe_float(m.get("rewardsMinSize")),
        # daily_rate populated by gamma_markets() from CLOB rewards programs
        "rewards_daily_rate":   None,
        "best_bid": _safe_float(m.get("bestBid")),
        "best_ask": _safe_float(m.get("bestAsk")),
        "last_trade_price": _safe_float(m.get("lastTradePrice")),
        "volume_24h": _safe_float(m.get("volume24hr")),
        "end_date": m.get("endDate") or "",
        "active": bool(m.get("active") or False),
        "closed": bool(m.get("closed") or False),
        "archived": bool(m.get("archived") or False),
    }


def fetch_book(token_id):
    """Public CLOB orderbook for one token. Returns dict with bids/asks
    sorted from best to worst, or None on error.
        bids: highest price first
        asks: lowest price first
    """
    try:
        r = requests.get(f"{CLOB_HOST}/book", params={"token_id": token_id},
                         headers=HDRS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        d = r.json() or {}
        bids = [{"price": float(x["price"]), "size": float(x["size"])}
                for x in (d.get("bids") or [])]
        asks = [{"price": float(x["price"]), "size": float(x["size"])}
                for x in (d.get("asks") or [])]
        bids.sort(key=lambda x: -x["price"])
        asks.sort(key=lambda x:  x["price"])
        return {
            "token_id": token_id,
            "bids": bids,
            "asks": asks,
            "tick_size": _safe_float(d.get("tick_size")) or 0.01,
            "min_order_size": _safe_float(d.get("min_order_size")) or 5.0,
            "neg_risk": bool(d.get("neg_risk") or False),
            "ts": time.time(),
        }
    except Exception as e:
        return {"token_id": token_id, "error": str(e), "bids": [], "asks": [],
                "tick_size": 0.01, "min_order_size": 5.0, "ts": time.time()}


def fetch_books_parallel(token_ids, workers=32):
    """Polymarket's CLOB tolerates 32-way parallel /book calls without
    rate-limiting in practice; raise if you've widened the snapshot to
    cover thousands of markets and refresh has gotten too slow."""
    out = {}
    if not token_ids: return out
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for tid, book in zip(token_ids, ex.map(fetch_book, token_ids)):
            out[tid] = book
    return out


# ---------------------------------------------------------------------------
# Reward score math.
# ---------------------------------------------------------------------------

def adjusted_midpoint(book, min_size):
    """Polymarket's reward-formula midpoint walks the book past dust orders
    smaller than min_size before computing the mid. We approximate that by
    finding the best bid/ask after skipping levels with size < min_size.
    Returns (mid_probability, best_bid_skipped, best_ask_skipped) or
    (None, None, None) if either side is empty / one-sided."""
    if not book or book.get("error"):
        return None, None, None
    bb = next((lvl["price"] for lvl in (book.get("bids") or [])
               if lvl["size"] >= min_size), None)
    ba = next((lvl["price"] for lvl in (book.get("asks") or [])
               if lvl["size"] >= min_size), None)
    if bb is None or ba is None:
        return None, bb, ba
    return 0.5 * (bb + ba), bb, ba


def score_one(price, size, mid_prob, max_spread_c, min_size):
    """Single-order score. price + mid in probability [0,1], max_spread in
    cents (¢ from mid), size in shares. Returns score, or 0.0 if ineligible."""
    if mid_prob is None or max_spread_c is None or max_spread_c <= 0:
        return 0.0
    if size < min_size:
        return 0.0
    s_cents = max(0.0, (mid_prob - price) * 100.0)
    if s_cents > max_spread_c:
        return 0.0
    w = (max_spread_c - s_cents) / max_spread_c
    return (w * w) * size


def score_book_side(book, mid_prob, max_spread_c, min_size):
    """Approximate the total score on one side of one market by treating
    each visible bid level (size >= min_size) as one eligible order. This
    is a lower-bound on the true total Q (which is over actual orders,
    not aggregated levels), but it's the only thing visible to a maker."""
    total = 0.0
    if not book or book.get("error"): return total
    for lvl in (book.get("bids") or []):
        total += score_one(lvl["price"], lvl["size"], mid_prob, max_spread_c, min_size)
    return total


def market_q(q_yes, q_no, mid_yes):
    """Per-sample maker Q for a market given side scores and YES midpoint."""
    if mid_yes is None: return 0.0
    if mid_yes < MID_LO or mid_yes > MID_HI:
        return min(q_yes, q_no)  # two-sided required outside [0.10, 0.90]
    return max(min(q_yes, q_no), max(q_yes, q_no) / REWARD_C)


# ---------------------------------------------------------------------------
# Authenticated CLOB calls (resting orders, place, cancel).
# ---------------------------------------------------------------------------

def my_open_orders():
    """All resting orders for our funder, keyed by token_id."""
    c = get_clob()
    if c is None: return {"by_token": {}, "all": []}
    try:
        orders = c.get_orders() or []
    except Exception as e:
        return {"by_token": {}, "all": [], "error": str(e)}
    by_token = {}
    for o in orders:
        tid = str(o.get("asset_id") or o.get("token_id") or "")
        by_token.setdefault(tid, []).append({
            "id":         o.get("id"),
            "token_id":   tid,
            "side":       (o.get("side") or "").upper(),
            "price":      _safe_float(o.get("price")),
            "size":       _safe_float(o.get("size_matched") and (
                              float(o.get("original_size") or 0) - float(o.get("size_matched") or 0)
                          ) or o.get("size") or o.get("original_size")),
            "original_size": _safe_float(o.get("original_size")),
            "size_matched":  _safe_float(o.get("size_matched")),
            "status":     o.get("status") or "",
            "created":    o.get("created_at") or "",
            "expiration": o.get("expiration") or "",
            "outcome":    o.get("outcome") or "",
        })
    return {"by_token": by_token, "all": orders}


def my_recent_fills(limit=200):
    c = get_clob()
    if c is None: return []
    try:
        fills = c.get_trades() or []
    except Exception:
        return []
    out = []
    for f in fills[:limit]:
        out.append({
            "id":        f.get("id"),
            "token_id":  str(f.get("asset_id") or ""),
            "side":      (f.get("side") or "").upper(),
            "price":     _safe_float(f.get("price")),
            "size":      _safe_float(f.get("size")),
            "fee":       _safe_float(f.get("fee_rate_bps") or 0),
            "created":   f.get("match_time") or f.get("created_at") or "",
            "outcome":   f.get("outcome") or "",
        })
    return out


def place_buy(token_id, price, size, post_only=True, neg_risk=False, tick_size=0.01):
    """Place a post-only GTC BUY at the given price/size. Returns the API
    response dict (or {'error': ...} on failure)."""
    c = get_clob()
    if c is None: return {"error": "clob client not configured"}
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
    from py_clob_client.order_builder.constants import BUY
    px = _round_to_tick(price, tick_size)
    args = OrderArgs(token_id=str(token_id), price=px, size=float(size), side=BUY)
    try:
        opts = PartialCreateOrderOptions(neg_risk=bool(neg_risk))
        signed = c.create_order(args, opts)
        resp = c.post_order(signed, OrderType.GTC, bool(post_only))
        return resp or {"ok": True}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def cancel_order(order_id):
    c = get_clob()
    if c is None: return {"error": "clob client not configured"}
    try:
        return c.cancel(order_id) or {"ok": True}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def cancel_orders_by_token(token_id):
    c = get_clob()
    if c is None: return {"error": "clob client not configured"}
    try:
        return c.cancel_market_orders(market="", asset_id=str(token_id)) or {"ok": True}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _round_to_tick(p, tick):
    if tick is None or tick <= 0: tick = 0.01
    p = max(tick, min(1.0 - tick, float(p)))
    return round(round(p / tick) * tick, 6)


# ---------------------------------------------------------------------------
# State files (followed markets, blocklist, theos).
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
    except Exception:
        return default


def _save_json(path, data):
    try:
        Path(path).write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def load_followed():
    return list(_load_json(FOLLOWED_PATH, []))


def save_followed(slugs):
    _save_json(FOLLOWED_PATH, sorted(set(slugs)))


def load_blocked():
    return set(_load_json(BLOCKED_PATH, []))


def save_blocked(s):
    _save_json(BLOCKED_PATH, sorted(s))


_theos_cache = {"map": {}, "meta": {}, "mtime": 0.0}


def load_theos():
    """Aggregate every theos/<slug>.json into a flat {slug: theo_yes_prob} map.
    Each file: {"slug": "...", "theo_yes": 0.42, "as_of": ..., ...}.
    Files can also map condition_id → theo_yes via a "markets" object:
        {"markets": {"<slug>": 0.42, ...}}
    Cache invalidated by max mtime of theos dir."""
    if not THEOS_DIR.exists():
        return {}, {}
    mtime = max((p.stat().st_mtime for p in THEOS_DIR.glob("*.json")), default=0.0)
    if mtime == _theos_cache["mtime"]:
        return _theos_cache["map"], _theos_cache["meta"]
    out_map, out_meta = {}, {}
    for p in THEOS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        slug = d.get("slug") or p.stem
        if "theo_yes" in d:
            out_map[slug] = float(d["theo_yes"])
            out_meta[slug] = {k: d.get(k) for k in ("as_of", "method", "confidence", "source")}
        if isinstance(d.get("markets"), dict):
            for k, v in d["markets"].items():
                try: out_map[k] = float(v)
                except Exception: continue
    _theos_cache["map"] = out_map
    _theos_cache["meta"] = out_meta
    _theos_cache["mtime"] = mtime
    return out_map, out_meta


# ---------------------------------------------------------------------------
# Snapshot — single in-memory view of {markets, books, my_orders, theos}.
# ---------------------------------------------------------------------------

class Snapshot:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {
            "as_of": 0.0,
            "rows": [],
            "markets_count": 0,
            "fetch_ms": 0,
            "errors": [],
            "fills": [],
            "my_clob_configured": False,
        }

    def set(self, **kw):
        with self.lock:
            self.data.update(kw)
            self.data["as_of"] = time.time()

    def get(self):
        with self.lock:
            return dict(self.data)


SNAPSHOT = Snapshot()


def build_rows(markets, books, my_orders, theos, blocked):
    """Compute a row per market with score / share / $/hr estimates."""
    rows = []
    for m in markets:
        slug = m["slug"]
        yes_tok, no_tok = m["yes_token"], m["no_token"]
        if not yes_tok or not no_tok:
            continue
        y_book = books.get(yes_tok) or {}
        n_book = books.get(no_tok)  or {}
        v = m.get("rewards_max_spread_c") or 0.0
        ms = m.get("rewards_min_size") or 0.0
        rate = m.get("rewards_daily_rate") or 0.0

        yes_mid, ybb, yba = adjusted_midpoint(y_book, ms)
        no_mid,  nbb, nba = adjusted_midpoint(n_book, ms)

        my_y = my_orders.get(yes_tok, [])
        my_n = my_orders.get(no_tok,  [])

        my_q_yes = sum(score_one(o["price"], o["size"], yes_mid, v, ms)
                       for o in my_y if (o["side"] == "BUY" and o["price"] is not None))
        my_q_no  = sum(score_one(o["price"], o["size"], no_mid,  v, ms)
                       for o in my_n if (o["side"] == "BUY" and o["price"] is not None))
        my_q = market_q(my_q_yes, my_q_no, yes_mid)

        # Total Q estimate from visible book (treat each level as one order).
        tot_q_yes = score_book_side(y_book, yes_mid, v, ms)
        tot_q_no  = score_book_side(n_book, no_mid,  v, ms)
        # Conservative cap: if we have orders at a level, our part of that level
        # is already counted in tot_q_*, so don't double-count.
        tot_q = max(my_q, market_q(tot_q_yes, tot_q_no, yes_mid))

        share = (my_q / tot_q) if tot_q > 0 else 0.0
        usd_per_day = share * rate
        usd_per_hr = usd_per_day / 24.0

        # competing_q = visible-book Q from other makers (lower bound, since
        # the aggregated CLOB book can't distinguish our orders from peers'
        # at the same level). When this is ~0 the market is uncontested,
        # i.e., posting min_size at the optimal price captures ~100% of the
        # daily pool. `if_alone_per_day` is the upper-bound rewards if so.
        competing_q = max(0.0, tot_q - my_q)
        EPS = 1e-6
        uncontested = competing_q <= EPS
        if_alone_per_day = rate if uncontested else 0.0
        if_alone_per_hr  = if_alone_per_day / 24.0

        theo_yes = theos.get(slug)
        theo_y_c = theo_yes * 100.0 if theo_yes is not None else None
        edge_y = (theo_y_c - (ybb * 100.0)) if (theo_y_c is not None and ybb is not None) else None
        edge_n = ((100.0 - theo_y_c) - (nbb * 100.0)) if (theo_y_c is not None and nbb is not None) else None

        rows.append({
            "slug": slug,
            "question": m["question"],
            "event_slug": m.get("event_slug") or "",
            "condition_id": m["condition_id"],
            "yes_token": yes_tok,
            "no_token":  no_tok,
            "neg_risk":  m.get("neg_risk", False),
            "tick_size": (y_book.get("tick_size") or 0.01),
            "min_order_size": (y_book.get("min_order_size") or 5.0),

            "v_cents":        v,
            "min_size":       ms,
            "daily_rate":     rate,

            "yes_best_bid": ybb, "yes_best_ask": yba, "yes_mid": yes_mid,
            "no_best_bid":  nbb, "no_best_ask":  nba, "no_mid":  no_mid,

            "my_yes_orders": my_y,
            "my_no_orders":  my_n,
            "my_yes_total":  sum(o.get("size") or 0 for o in my_y),
            "my_no_total":   sum(o.get("size") or 0 for o in my_n),
            "my_yes_top_px": max((o["price"] for o in my_y if o.get("price") is not None), default=None),
            "my_no_top_px":  max((o["price"] for o in my_n if o.get("price") is not None), default=None),

            "my_q_yes": my_q_yes, "my_q_no": my_q_no, "my_q": my_q,
            "tot_q":    tot_q,
            "competing_q":      competing_q,
            "uncontested":      uncontested,
            "share":    share,
            "usd_per_day": usd_per_day,
            "usd_per_hr":  usd_per_hr,
            "if_alone_per_day": if_alone_per_day,
            "if_alone_per_hr":  if_alone_per_hr,

            "theo_yes":  theo_yes,
            "theo_y_c":  theo_y_c,
            "edge_yes":  edge_y,
            "edge_no":   edge_n,

            "blocked":   slug in blocked,
            "end_date":  m.get("end_date") or "",
        })
    rows.sort(key=lambda r: (-r["usd_per_hr"], -(r["daily_rate"] or 0), r["slug"]))
    return rows


_top_n_override = {"value": None}
_min_pool_override = {"value": None}


def refresh_snapshot():
    t0 = time.time()
    errors = []
    try:
        followed = set(load_followed())
        blocked = load_blocked()
        try:
            mkts = gamma_markets(rewards_only=True,
                                 top_n=_top_n_override["value"],
                                 min_pool_usd=_min_pool_override["value"],
                                 followed_slugs=followed)
        except Exception as e:
            mkts = []
            errors.append(f"gamma: {e}")
        token_ids = []
        for m in mkts:
            if m["yes_token"]: token_ids.append(m["yes_token"])
            if m["no_token"]:  token_ids.append(m["no_token"])
        books = fetch_books_parallel(token_ids, workers=16)
        my = my_open_orders()
        my_orders = my["by_token"]
        if my.get("error"): errors.append(f"orders: {my['error']}")
        fills = my_recent_fills(limit=100)
        theos, _ = load_theos()
        rows = build_rows(mkts, books, my_orders, theos, blocked)
        SNAPSHOT.set(rows=rows, markets_count=len(mkts), errors=errors,
                     fetch_ms=int((time.time() - t0) * 1000), fills=fills,
                     my_clob_configured=(get_clob() is not None))
    except Exception as e:
        errors.append(f"refresh: {e}")
        SNAPSHOT.set(errors=errors)


def snapshot_loop(interval):
    while True:
        try:
            refresh_snapshot()
        except Exception:
            pass
        time.sleep(max(0.5, interval))


# ---------------------------------------------------------------------------
# Auto-pennying bot (defensive: keeps a min_size order on each side near mid).
# ---------------------------------------------------------------------------

class PennyBot(threading.Thread):
    """Background thread that, for each rewarded market not blocked:

      1. Reads the latest snapshot row (no API hits of its own).
      2. Targets two-sided liquidity: place_buy(YES, mid_yes - max_spread/2)
         and place_buy(NO,  mid_no  - max_spread/2), at min_size shares each.
      3. Skips if we already have an order within `tolerance_c` cents of the
         target price.
      4. Skips if the place would cross the best_ask (post-only would reject
         anyway, but we save the API call).
      5. Re-enters from cooldown when displaced (mid moved enough that our
         distance from mid > defend_distance_c).
    """

    def __init__(self, interval=30, place_offset_c=2.0, tolerance_c=1.0,
                 defend_distance_c=None, cooldown_s=180, defend_cooldown_s=20,
                 enabled=False):
        super().__init__(daemon=True)
        self.interval = interval
        self.place_offset_c = place_offset_c
        self.tolerance_c = tolerance_c
        self.defend_distance_c = defend_distance_c  # None → 0.8 * v_cents
        self.cooldown_s = cooldown_s
        self.defend_cooldown_s = defend_cooldown_s
        self.enabled = enabled
        self.last_place = {}   # (slug, side) -> ts
        self.events = []       # ring buffer of recent decisions, capped
        self.lock = threading.Lock()

    def emit(self, msg):
        with self.lock:
            self.events.append((time.time(), msg))
            if len(self.events) > 200:
                self.events = self.events[-200:]

    def recent_events(self, n=80):
        with self.lock:
            return list(self.events[-n:])

    def _decide_one(self, row, side):
        slug = row["slug"]
        v = row["v_cents"] or 0.0
        ms = row["min_size"] or 0.0
        if v <= 0 or ms <= 0:
            return ("skip", "no rewards params")
        if row["blocked"]:
            return ("skip", "blocked")
        token_id = row["yes_token"] if side == "yes" else row["no_token"]
        mid_p = row["yes_mid"] if side == "yes" else row["no_mid"]
        best_ask = row["yes_best_ask"] if side == "yes" else row["no_best_ask"]
        my_top_px = row["my_yes_top_px"] if side == "yes" else row["my_no_top_px"]
        my_total = row["my_yes_total"]  if side == "yes" else row["my_no_total"]
        if mid_p is None:
            return ("skip", "no mid")
        # target price: mid - (place_offset / 100); enforce within max_spread.
        offset_c = max(0.5, min(self.place_offset_c, max(1.0, v * 0.6)))
        target_p = mid_p - (offset_c / 100.0)
        if best_ask is not None and target_p >= best_ask:
            return ("skip", f"target {target_p:.4f} would cross ask {best_ask:.4f}")
        target_p = _round_to_tick(target_p, row["tick_size"] or 0.01)
        # Already-placed check.
        defend_d = self.defend_distance_c if self.defend_distance_c is not None else 0.8 * v
        my_dist_c = ((mid_p - my_top_px) * 100.0) if my_top_px is not None else None
        already_close = (my_total >= ms) and (my_top_px is not None) \
                        and abs((my_top_px - target_p) * 100.0) <= self.tolerance_c
        is_defense = (my_top_px is None) or (my_dist_c is not None and my_dist_c > defend_d)
        if already_close and not is_defense:
            return ("skip", f"already at {my_top_px:.4f}")
        cooldown = self.defend_cooldown_s if is_defense else self.cooldown_s
        now = time.time()
        last = self.last_place.get((slug, side), 0.0)
        if now - last < cooldown:
            return ("skip", f"cooldown {int(cooldown - (now-last))}s")
        # Place.
        size = float(ms)
        resp = place_buy(token_id, target_p, size,
                         post_only=True, neg_risk=row["neg_risk"],
                         tick_size=row["tick_size"] or 0.01)
        self.last_place[(slug, side)] = now
        if resp.get("error"):
            return ("error", f"{side} {target_p:.4f} x{size:g}: {resp['error']}")
        return ("place", f"{side} {target_p:.4f} x{size:g}")

    def run(self):
        while True:
            try:
                if not self.enabled:
                    time.sleep(min(2.0, self.interval))
                    continue
                if get_clob() is None:
                    self.emit("clob not configured — set POLYMARKET_PRIVATE_KEY")
                    time.sleep(self.interval)
                    continue
                snap = SNAPSHOT.get()
                for row in snap.get("rows", []):
                    if row.get("blocked"): continue
                    for side in ("yes", "no"):
                        action, msg = self._decide_one(row, side)
                        if action != "skip":
                            self.emit(f"{row['slug']} {action}: {msg}")
            except Exception as e:
                self.emit(f"loop error: {e}")
            time.sleep(self.interval)


PENNY_BOT = PennyBot()


# ---------------------------------------------------------------------------
# HTTP server.
# ---------------------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html><head><meta charset='utf-8'><title>Polymarket Rewards</title>
<style>
 body { background:#0d1117; color:#e6edf3; font:13px/1.4 -apple-system,Helvetica,sans-serif; margin:0; }
 a { color:#79c0ff; text-decoration:none; }
 a:hover { text-decoration:underline; }
 header { padding:10px 16px; background:#161b22; border-bottom:1px solid #30363d;
          display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
 header h1 { margin:0; font-size:16px; font-weight:600; }
 .stat { color:#8b949e; }
 .stat b { color:#e6edf3; }
 .panel { padding:10px 16px; background:#0d1117; border-bottom:1px solid #21262d; }
 .panel h3 { margin:0 0 6px; font-size:12px; color:#8b949e; font-weight:600;
             text-transform:uppercase; letter-spacing:0.06em; }
 input, select, button { background:#0d1117; color:#e6edf3; border:1px solid #30363d;
                         border-radius:6px; padding:5px 9px; font:inherit; }
 button { cursor:pointer; }
 button:hover { background:#21262d; }
 button.primary { background:#238636; border-color:#2ea043; }
 button.primary:hover { background:#2ea043; }
 button.danger { background:#da3633; border-color:#f85149; }
 button.danger:hover { background:#f85149; }
 button[disabled] { opacity:0.4; cursor:not-allowed; }
 table { width:100%; border-collapse:collapse; font-size:12px; }
 th, td { padding:6px 8px; border-bottom:1px solid #21262d; text-align:right; vertical-align:top; }
 th { background:#161b22; color:#8b949e; font-weight:600; position:sticky; top:0;
      text-align:right; cursor:pointer; user-select:none; }
 th.l, td.l { text-align:left; }
 tr.blocked { opacity:0.45; }
 tr:hover { background:#161b22; }
 .badge { display:inline-block; padding:1px 5px; border-radius:3px; font-size:10px;
          font-weight:600; letter-spacing:0.04em; }
 .b-yes { background:#1f6feb33; color:#79c0ff; }
 .b-no  { background:#da363322; color:#ffa198; }
 .b-rew { background:#23863622; color:#7ee787; }
 .b-blk { background:#6e768133; color:#8b949e; }
 .pos { color:#7ee787; }
 .neg { color:#ffa198; }
 .dim { color:#6e7681; }
 .small { font-size:11px; }
 .events { max-height:160px; overflow:auto; font-family:Menlo,monospace;
           font-size:11px; padding:6px; background:#010409; border:1px solid #30363d;
           border-radius:4px; }
 .events div { padding:1px 0; color:#8b949e; }
 .question { max-width:340px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
</style></head><body>
<header>
  <h1>Polymarket Rewards</h1>
  <span class='stat'>markets <b id='nmkt'>—</b></span>
  <span class='stat'>$/hr est <b id='hr'>—</b></span>
  <span class='stat'>$/day est <b id='day'>—</b></span>
  <span class='stat'>my Q <b id='myq'>—</b></span>
  <span class='stat'>fetch <b id='fms'>—</b>ms</span>
  <span class='stat'>as of <b id='asof'>—</b></span>
  <span class='stat' id='clobstat'></span>
  <span style='flex:1'></span>
  <label><input type='checkbox' id='hidezero'> hide $/hr=0</label>
  <label><input type='checkbox' id='uncontested'> uncontested only (no other makers)</label>
  <button onclick='refresh()'>↻ refresh</button>
</header>
<div class='panel'>
  <h3>Auto-Pennying Bot</h3>
  <div style='display:flex; gap:10px; flex-wrap:wrap; align-items:center;'>
    <button id='bot_btn' class='primary' onclick='toggleBot()'>Start</button>
    <span class='stat'>state <b id='bot_state'>off</b></span>
    <span class='stat'>interval <b id='bot_int'>—</b>s</span>
    <span class='stat'>offset <b id='bot_off'>—</b>¢</span>
    <span class='stat'>tolerance <b id='bot_tol'>—</b>¢</span>
    <button onclick='cfgBot()'>Configure…</button>
    <button class='danger' onclick='cancelAll()'>Cancel ALL my orders</button>
  </div>
  <div class='events' id='evlog'></div>
</div>
<div class='panel'>
  <h3>Follow / Search</h3>
  <input id='search' placeholder='market slug or substring…' style='width:340px'
    onkeydown='if(event.key=="Enter") doSearch()'/>
  <button onclick='doSearch()'>Search live markets</button>
  <span id='searchres' class='small dim'></span>
</div>
<div id='wrap'></div>
<script>
let cfg = { token: null };
let sortKey = 'usd_per_hr', sortDir = -1;
function fmtN(x, d=2) { if (x==null||isNaN(x)) return '—'; return Number(x).toFixed(d); }
function fmtP(x, d=1) { if (x==null||isNaN(x)) return '—'; return (x*100).toFixed(d)+'¢'; }
function fmtPct(x, d=1) { if (x==null||isNaN(x)) return '—'; return (x*100).toFixed(d)+'%'; }
function fmtUsd(x, d=2) { if (x==null||isNaN(x)) return '—'; return '$'+Number(x).toFixed(d); }
function relTime(t) { let d = (Date.now()/1000) - t; return d<2?'now':Math.round(d)+'s ago'; }
async function getJSON(p) { let r = await fetch(p); return r.json(); }
async function postJSON(p, body) {
  let r = await fetch(p, {method:'POST', headers:{'Content-Type':'application/json',
                          'X-Mutation-Token': cfg.token},
                          body: JSON.stringify(body||{})});
  return r.json();
}
async function init() {
  let s = await getJSON('/state');
  cfg.token = s.token;
}
async function refresh() {
  let s = await getJSON('/snapshot');
  let rows = s.rows || [];
  let uncontestedOnly = document.getElementById('uncontested').checked;
  if (uncontestedOnly) {
    rows = rows.filter(r => r.uncontested);
    // When filtering to uncontested, default to ranking by largest pool.
    if (sortKey === 'usd_per_hr') { sortKey = 'if_alone_per_hr'; sortDir = -1; }
  }
  if (document.getElementById('hidezero').checked)
    rows = rows.filter(r => (r.usd_per_hr||0) > 0 || (r.my_yes_total||0) > 0 || (r.my_no_total||0) > 0
                          || (uncontestedOnly && (r.daily_rate||0) > 0));
  rows.sort((a,b) => sortDir * ((a[sortKey]??0) - (b[sortKey]??0)));
  document.getElementById('nmkt').textContent = rows.length;
  document.getElementById('hr').textContent = fmtUsd(rows.reduce((s,r)=>s+(r.usd_per_hr||0),0));
  document.getElementById('day').textContent = fmtUsd(rows.reduce((s,r)=>s+(r.usd_per_day||0),0));
  document.getElementById('myq').textContent = fmtN(rows.reduce((s,r)=>s+(r.my_q||0),0), 1);
  document.getElementById('fms').textContent = s.fetch_ms;
  document.getElementById('asof').textContent = relTime(s.as_of);
  document.getElementById('clobstat').innerHTML = s.my_clob_configured
      ? "<span class='b-rew badge'>signing key loaded</span>"
      : "<span class='b-blk badge'>signing key NOT loaded</span>";
  let bot = await getJSON('/bot/state');
  document.getElementById('bot_btn').textContent = bot.enabled ? 'Stop' : 'Start';
  document.getElementById('bot_btn').className = bot.enabled ? 'danger' : 'primary';
  document.getElementById('bot_state').textContent = bot.enabled ? 'on' : 'off';
  document.getElementById('bot_int').textContent = bot.interval;
  document.getElementById('bot_off').textContent = bot.place_offset_c;
  document.getElementById('bot_tol').textContent = bot.tolerance_c;
  let log = document.getElementById('evlog');
  log.innerHTML = (bot.events||[]).slice(-40).reverse().map(e =>
      "<div>"+new Date(e[0]*1000).toLocaleTimeString()+" — "+escape(e[1])+"</div>").join('');
  let cols = [
    ['slug','market', 'l'],
    ['daily_rate','Rewards $/day','r'],
    ['v_cents','Max Spread','r'],
    ['min_size','Min Shares','r'],
    ['yes_mid','YES mid','r'], ['no_mid','NO mid','r'],
    ['my_yes_total','my YES','r'], ['my_no_total','my NO','r'],
    ['my_q','my Q','r'], ['competing_q','others Q','r'],
    ['share','share','r'],
    ['usd_per_hr','$/hr','r'],
    ['if_alone_per_hr','$/hr alone','r'],
    ['theo_y_c','theo YES¢','r'], ['edge_yes','edge YES','r'],
    ['', 'actions', 'l']
  ];
  let html = "<table><thead><tr>" + cols.map(c =>
      "<th class='"+c[2]+"' onclick=\\"setSort('"+c[0]+"')\\">"+c[1]+(sortKey==c[0]?(sortDir>0?' ▲':' ▼'):'')+"</th>"
  ).join('') + "</tr></thead><tbody>";
  for (let r of rows) {
    let cls = r.blocked ? 'blocked' : '';
    html += "<tr class='"+cls+"'>";
    html += "<td class='l'><a target='_blank' href='https://polymarket.com/market/"+escape(r.slug)+"'>"
            + "<span class='question' title='"+escape(r.question)+"'>"+escape(r.question||r.slug)+"</span></a>"
            + "<br><span class='small dim'>"+escape(r.slug)
            + (r.blocked? "&nbsp;<span class='badge b-blk'>BLOCKED</span>":"")
            + (r.neg_risk? "&nbsp;<span class='badge b-blk'>NEG-RISK</span>":"")
            +"</span></td>";
    html += "<td>"+fmtUsd(r.daily_rate,1)+"</td>";
    html += "<td>±"+fmtN(r.v_cents,1)+"¢</td>";
    html += "<td>"+fmtN(r.min_size,0)+"</td>";
    html += "<td>"+fmtP(r.yes_mid)+"<br><span class='small dim'>"+fmtP(r.yes_best_bid)+" / "+fmtP(r.yes_best_ask)+"</span></td>";
    html += "<td>"+fmtP(r.no_mid)+"<br><span class='small dim'>"+fmtP(r.no_best_bid)+" / "+fmtP(r.no_best_ask)+"</span></td>";
    html += "<td>"+fmtN(r.my_yes_total,0)+(r.my_yes_top_px!=null?"<br><span class='small dim'>@"+fmtP(r.my_yes_top_px)+"</span>":"")+"</td>";
    html += "<td>"+fmtN(r.my_no_total,0)+(r.my_no_top_px!=null?"<br><span class='small dim'>@"+fmtP(r.my_no_top_px)+"</span>":"")+"</td>";
    html += "<td>"+fmtN(r.my_q,1)+"</td>";
    let othersCls = r.uncontested ? 'pos' : '';
    html += "<td class='"+othersCls+"'>"+(r.uncontested ? "<b>0</b>" : fmtN(r.competing_q,1))+"</td>";
    html += "<td>"+fmtPct(r.share)+"</td>";
    html += "<td><b>"+fmtUsd(r.usd_per_hr,3)+"</b><br><span class='small dim'>"+fmtUsd(r.usd_per_day,2)+"/d</span></td>";
    let aloneCls = (r.if_alone_per_hr||0) > 0 ? 'pos' : 'dim';
    html += "<td class='"+aloneCls+"'>"+(r.uncontested ? "<b>"+fmtUsd(r.if_alone_per_hr,3)+"</b><br><span class='small dim'>"+fmtUsd(r.if_alone_per_day,2)+"/d</span>" : '—')+"</td>";
    html += "<td>"+fmtN(r.theo_y_c,1)+"</td>";
    let edgecls = r.edge_yes==null ? '' : (r.edge_yes>0?'pos':'neg');
    html += "<td class='"+edgecls+"'>"+(r.edge_yes!=null?(r.edge_yes>0?'+':'')+fmtN(r.edge_yes,1):'—')+"</td>";
    html += "<td class='l'>"
        + "<button onclick=\\"placePair('"+escape(r.slug)+"')\\">place ±</button> "
        + "<button onclick=\\"cancelMkt('"+escape(r.slug)+"')\\">cancel</button> "
        + "<button onclick=\\"toggleBlock('"+escape(r.slug)+"')\\">"+(r.blocked?'unblock':'⛔ block')+"</button>"
        + "</td>";
    html += "</tr>";
  }
  html += "</tbody></table>";
  document.getElementById('wrap').innerHTML = html;
}
function escape(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,"&#39;"); }
function setSort(k) {
  if (!k) return;
  if (sortKey === k) sortDir = -sortDir; else { sortKey = k; sortDir = -1; }
  refresh();
}
async function toggleBot() {
  let s = await postJSON('/bot/toggle');
  refresh();
}
async function cfgBot() {
  let i = prompt("Interval seconds (current "+document.getElementById('bot_int').textContent+"):", "30");
  let o = prompt("Place offset ¢ from mid (current "+document.getElementById('bot_off').textContent+"):", "2.0");
  let t = prompt("Tolerance ¢ (skip if already within this far of target, current "+document.getElementById('bot_tol').textContent+"):", "1.0");
  await postJSON('/bot/config', {interval:Number(i), place_offset_c:Number(o), tolerance_c:Number(t)});
  refresh();
}
async function placePair(slug) {
  if (!confirm("Place YES + NO post-only orders at min_size on "+slug+"?")) return;
  let r = await postJSON('/place_pair', {slug:slug});
  alert(JSON.stringify(r, null, 2));
  refresh();
}
async function cancelMkt(slug) {
  if (!confirm("Cancel all orders on "+slug+"?")) return;
  await postJSON('/cancel_market', {slug:slug});
  refresh();
}
async function cancelAll() {
  if (!confirm("Cancel ALL resting orders across every market?")) return;
  await postJSON('/cancel_all');
  refresh();
}
async function toggleBlock(slug) {
  await postJSON('/block_toggle', {slug:slug});
  refresh();
}
async function doSearch() {
  let q = document.getElementById('search').value.trim();
  if (!q) return;
  let r = await getJSON('/search?q='+encodeURIComponent(q));
  let html = (r.results||[]).map(m =>
    "<div><a target='_blank' href='https://polymarket.com/market/"+escape(m.slug)+"'>"+escape(m.question)+"</a> "
    + "<span class='dim small'>("+escape(m.slug)+", $"+fmtN(m.rewards_daily_rate,1)+"/d)</span> "
    + "<button onclick=\\"follow('"+escape(m.slug)+"')\\">follow</button></div>"
  ).join('');
  document.getElementById('searchres').innerHTML = html || '<span class=dim>no matches</span>';
}
async function follow(slug) {
  await postJSON('/follow', {slug:slug});
  refresh();
}
init().then(refresh);
setInterval(refresh, 4000);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a, **kw): pass

    def _send(self, code, body, content_type="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_token(self):
        tok = self.headers.get("X-Mutation-Token", "")
        if tok != LOCAL_MUTATION_TOKEN:
            self._send(403, {"error": "bad token"})
            return False
        return True

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0: return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/" or p == "/index.html":
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            return
        if p == "/state":
            self._send(200, {"token": LOCAL_MUTATION_TOKEN})
            return
        if p == "/snapshot":
            self._send(200, SNAPSHOT.get())
            return
        if p == "/bot/state":
            self._send(200, {
                "enabled": PENNY_BOT.enabled,
                "interval": PENNY_BOT.interval,
                "place_offset_c": PENNY_BOT.place_offset_c,
                "tolerance_c": PENNY_BOT.tolerance_c,
                "cooldown_s": PENNY_BOT.cooldown_s,
                "defend_cooldown_s": PENNY_BOT.defend_cooldown_s,
                "events": PENNY_BOT.recent_events(40),
            })
            return
        if p == "/search":
            q = (parse_qs(u.query).get("q") or [""])[0].lower()
            if not q:
                self._send(200, {"results": []}); return
            try:
                results = []
                for m in gamma_markets(rewards_only=True):
                    if q in (m.get("slug") or "").lower() or q in (m.get("question") or "").lower():
                        results.append(m)
                    if len(results) >= 50: break
                self._send(200, {"results": results})
            except Exception as e:
                self._send(500, {"error": str(e)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_token(): return
        u = urlparse(self.path)
        p = u.path
        body = self._read_json()
        if p == "/bot/toggle":
            PENNY_BOT.enabled = not PENNY_BOT.enabled
            self._send(200, {"enabled": PENNY_BOT.enabled})
            return
        if p == "/bot/config":
            for k in ("interval", "place_offset_c", "tolerance_c",
                      "cooldown_s", "defend_cooldown_s"):
                if k in body and body[k] is not None:
                    setattr(PENNY_BOT, k, type(getattr(PENNY_BOT, k))(body[k]))
            self._send(200, {"ok": True})
            return
        if p == "/place_pair":
            slug = body.get("slug")
            row = next((r for r in SNAPSHOT.get().get("rows", []) if r["slug"] == slug), None)
            if row is None:
                self._send(404, {"error": "no such market in snapshot"}); return
            results = {}
            for side in ("yes", "no"):
                action, msg = PENNY_BOT._decide_one(row, side)
                results[side] = {"action": action, "msg": msg}
            self._send(200, results)
            return
        if p == "/cancel_market":
            slug = body.get("slug")
            row = next((r for r in SNAPSHOT.get().get("rows", []) if r["slug"] == slug), None)
            if row is None:
                self._send(404, {"error": "no such market"}); return
            r1 = cancel_orders_by_token(row["yes_token"])
            r2 = cancel_orders_by_token(row["no_token"])
            self._send(200, {"yes": r1, "no": r2})
            return
        if p == "/cancel_all":
            c = get_clob()
            if c is None:
                self._send(400, {"error": "clob not configured"}); return
            try:
                resp = c.cancel_all()
            except Exception as e:
                resp = {"error": f"{type(e).__name__}: {e}"}
            self._send(200, {"resp": resp})
            return
        if p == "/block_toggle":
            slug = body.get("slug")
            with _state_lock:
                blk = load_blocked()
                if slug in blk: blk.remove(slug)
                else: blk.add(slug)
                save_blocked(blk)
            self._send(200, {"blocked": slug in blk})
            return
        if p == "/follow":
            slug = body.get("slug")
            with _state_lock:
                f = set(load_followed()); f.add(slug); save_followed(list(f))
            self._send(200, {"ok": True})
            return
        if p == "/unfollow":
            slug = body.get("slug")
            with _state_lock:
                f = set(load_followed()); f.discard(slug); save_followed(list(f))
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--snapshot-interval", type=float, default=5.0,
                    help="seconds between full market refreshes")
    ap.add_argument("--bot-interval", type=float, default=30,
                    help="auto-pennying cycle period (seconds)")
    ap.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                    help=f"orderbook-fetch only the top N rewarded markets by "
                         f"daily rate (default {DEFAULT_TOP_N}); raise carefully — "
                         f"Polymarket has 5000+ programs, mostly tiny sports props")
    ap.add_argument("--min-pool", type=float, default=DEFAULT_MIN_POOL_USD,
                    help=f"skip rewards programs with daily pool below this many "
                         f"USDC (default ${DEFAULT_MIN_POOL_USD:g}/day)")
    args = ap.parse_args()

    PENNY_BOT.interval = args.bot_interval
    _top_n_override["value"] = args.top_n
    _min_pool_override["value"] = args.min_pool

    threading.Thread(target=snapshot_loop, args=(args.snapshot_interval,), daemon=True).start()
    PENNY_BOT.start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"polymarket rewards on http://{args.host}:{args.port}/")
    print(f"clob configured: {get_clob() is not None} | funder: {FUNDER or '(none)'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
