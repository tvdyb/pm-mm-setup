#!/usr/bin/env python3
"""Standalone rewards-eligibility enforcer.

Walks every open BUY order and cancels any that have drifted outside the
market's rewardsMaxSpread cents from the current adjusted midpoint. Such
orders score zero but still tie up capital — better to free that USDC and
re-place at a fresh price.

Predates the rewards-app's PennyBot and is useful as a belt-and-braces
process: run it as a separate cron / launchd job so even when the dashboard
is offline, dead orders get cleaned up.

Usage:
    python3 polymarket_reward_monitor.py --once
    python3 polymarket_reward_monitor.py --once --dry-run
    python3 polymarket_reward_monitor.py             # loops forever, default 60s
"""
import argparse, datetime as dt, json, os, sys, time
from pathlib import Path
import requests

CLOB_HOST  = os.environ.get("POLYMARKET_CLOB_HOST",  "https://clob.polymarket.com")
GAMMA_HOST = os.environ.get("POLYMARKET_GAMMA_HOST", "https://gamma-api.polymarket.com")
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
FUNDER      = os.environ.get("POLYMARKET_FUNDER", "")
SIG_TYPE    = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
CHAIN_ID    = 137
LOG_PATH = Path(os.environ.get("POLYMARKET_MONITOR_LOG",
                               "./polymarket_reward_monitor.log"))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
HDRS = {"User-Agent": UA, "Accept": "application/json"}


def log(msg, also_print=True):
    line = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}\n"
    try: LOG_PATH.parent.mkdir(parents=True, exist_ok=True); LOG_PATH.open("a").write(line)
    except Exception: pass
    if also_print: print(line, end="")


def safe_float(x):
    try: return float(x) if x is not None else None
    except (TypeError, ValueError): return None


def fetch_book(token_id):
    try:
        r = requests.get(f"{CLOB_HOST}/book", params={"token_id": token_id},
                         headers=HDRS, timeout=10)
        r.raise_for_status()
        d = r.json() or {}
        bids = sorted([(float(x["price"]), float(x["size"])) for x in d.get("bids") or []],
                      key=lambda t: -t[0])
        asks = sorted([(float(x["price"]), float(x["size"])) for x in d.get("asks") or []],
                      key=lambda t: t[0])
        return {"bids": bids, "asks": asks}
    except Exception as e:
        log(f"book err {token_id}: {e}", also_print=False)
        return None


def adjusted_mid(book, min_size):
    if not book: return None
    bb = next((p for p, s in book["bids"] if s >= min_size), None)
    ba = next((p for p, s in book["asks"] if s >= min_size), None)
    if bb is None or ba is None: return None
    return 0.5 * (bb + ba)


def gamma_market_by_token(token_id, _cache={}):
    """Reverse-lookup market metadata (rewards params) by token id. Caches
    per process. Polymarket Gamma's /markets endpoint doesn't filter by
    token, so we cache a {token_id: market} map seeded as we go."""
    if token_id in _cache: return _cache[token_id]
    try:
        r = requests.get(f"{GAMMA_HOST}/markets",
                         params={"clob_token_ids": token_id}, headers=HDRS, timeout=10)
        if r.status_code == 200:
            arr = r.json() or []
            if arr:
                _cache[token_id] = arr[0]
                return arr[0]
    except Exception:
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="run a single sweep and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print cancellations without sending them")
    ap.add_argument("--interval", type=float, default=60.0,
                    help="seconds between sweeps in loop mode")
    ap.add_argument("--slack-cents", type=float, default=0.0,
                    help="cancel only when distance exceeds (max_spread + this); "
                         "use 0 to cancel as soon as ineligible")
    args = ap.parse_args()

    if not (PRIVATE_KEY and FUNDER):
        log("ERROR: POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER must be set")
        sys.exit(1)

    from py_clob_client.client import ClobClient
    client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                        signature_type=SIG_TYPE, funder=FUNDER)
    client.set_api_creds(client.create_or_derive_api_creds())

    def sweep():
        try:
            orders = client.get_orders() or []
        except Exception as e:
            log(f"get_orders failed: {e}"); return
        if not orders:
            log("0 open orders"); return
        cancels = []
        for o in orders:
            tid = str(o.get("asset_id") or o.get("token_id") or "")
            if (o.get("side") or "").upper() != "BUY": continue
            px = safe_float(o.get("price"))
            sz = safe_float(o.get("size") or o.get("original_size"))
            if px is None or sz is None: continue
            m = gamma_market_by_token(tid)
            if not m:
                log(f"order {o.get('id')} on {tid}: market lookup failed (skipping)",
                    also_print=False); continue
            v = safe_float(m.get("rewardsMaxSpread"))
            ms = safe_float(m.get("rewardsMinSize"))
            if v is None or v <= 0:
                # Market no longer rewarded — cancel.
                cancels.append((o.get("id"), tid, "no rewards program", px, sz))
                continue
            if sz < (ms or 0):
                cancels.append((o.get("id"), tid,
                                f"size {sz} < min_size {ms}", px, sz)); continue
            book = fetch_book(tid)
            mid = adjusted_mid(book, ms or 0)
            if mid is None:
                continue  # one-sided book; don't cancel reflexively
            dist_c = (mid - px) * 100.0
            if dist_c > (v + args.slack_cents):
                cancels.append((o.get("id"), tid,
                                f"dist {dist_c:.2f}¢ > max_spread {v}¢", px, sz))
        log(f"swept {len(orders)} orders → {len(cancels)} ineligible")
        for oid, tid, reason, px, sz in cancels:
            if args.dry_run:
                log(f"DRY-CANCEL {oid} ({tid[:10]}…) px={px:.4f} sz={sz} — {reason}")
                continue
            try:
                client.cancel(oid)
                log(f"CANCEL {oid} ({tid[:10]}…) px={px:.4f} sz={sz} — {reason}")
            except Exception as e:
                log(f"cancel {oid} failed: {e}")
            time.sleep(0.15)

    if args.once:
        sweep(); return
    log(f"reward monitor: loop every {args.interval}s, slack={args.slack_cents}¢")
    while True:
        try: sweep()
        except Exception as e: log(f"sweep error: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
