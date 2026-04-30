#!/usr/bin/env python3
"""Bulk seed both sides of a Polymarket market (or every market in an event)
with post-only GTC limit orders at the min_size required by the rewards
program. Defaults to dry-run; pass --live to actually place.

Examples:
    # Single market by slug
    python3 polymarket_place_orders.py --slug will-trump-cabinet-pick-x-be-confirmed --offset 2

    # Every rewards-active market in an event slug
    python3 polymarket_place_orders.py --event-slug fed-may-rate-decision --offset 2 --live

    # Custom size and offset (default size = market's rewardsMinSize)
    python3 polymarket_place_orders.py --slug X --offset 3 --size 200 --live
"""
import argparse, json, os, sys, time
from pathlib import Path
import requests

CLOB_HOST  = os.environ.get("POLYMARKET_CLOB_HOST",  "https://clob.polymarket.com")
GAMMA_HOST = os.environ.get("POLYMARKET_GAMMA_HOST", "https://gamma-api.polymarket.com")
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
FUNDER      = os.environ.get("POLYMARKET_FUNDER", "")
SIG_TYPE    = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
CHAIN_ID    = 137

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
HDRS = {"User-Agent": UA, "Accept": "application/json"}


def _safe_float(x):
    try: return float(x) if x is not None else None
    except (TypeError, ValueError): return None


def gamma_markets_by_event(event_slug):
    r = requests.get(f"{GAMMA_HOST}/events", params={"slug": event_slug},
                     headers=HDRS, timeout=15)
    r.raise_for_status()
    arr = r.json() or []
    if not arr: return []
    return arr[0].get("markets") or []


def gamma_market(slug):
    r = requests.get(f"{GAMMA_HOST}/markets", params={"slug": slug},
                     headers=HDRS, timeout=15)
    r.raise_for_status()
    arr = r.json() or []
    return arr[0] if arr else None


def midpoint(client, token_id):
    try:
        r = client.get_midpoint(token_id)
        return _safe_float((r or {}).get("mid"))
    except Exception:
        return None


def tick_for(client, token_id):
    try:
        r = client.get_tick_size(token_id)
        return _safe_float(r) or 0.01
    except Exception:
        return 0.01


def round_to_tick(p, tick):
    if tick <= 0: tick = 0.01
    p = max(tick, min(1.0 - tick, float(p)))
    return round(round(p / tick) * tick, 6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", help="single market slug")
    ap.add_argument("--event-slug", help="seed every market in this event")
    ap.add_argument("--offset", type=float, default=2.0,
                    help="cents below mid to bid each side (default 2)")
    ap.add_argument("--size", type=float, default=None,
                    help="size override; default = market's rewardsMinSize")
    ap.add_argument("--live", action="store_true",
                    help="actually place (default dry-run)")
    ap.add_argument("--out", default=None, help="audit JSON dump path")
    args = ap.parse_args()

    if not args.slug and not args.event_slug:
        ap.error("--slug or --event-slug is required")
    if args.live and not (PRIVATE_KEY and FUNDER):
        ap.error("--live requires POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER set")

    # Resolve markets.
    if args.slug:
        m = gamma_market(args.slug)
        if not m:
            print(f"no such market: {args.slug}", file=sys.stderr); sys.exit(1)
        targets = [m]
    else:
        targets = gamma_markets_by_event(args.event_slug)

    targets = [m for m in targets if not m.get("closed") and not m.get("archived")]
    if not targets:
        print("no open markets matched", file=sys.stderr); sys.exit(1)

    # Init CLOB client.
    if args.live:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY
        client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                            signature_type=SIG_TYPE, funder=FUNDER)
        client.set_api_creds(client.create_or_derive_api_creds())
    else:
        client = None

    out_records = []
    for m in targets:
        slug = m.get("slug")
        question = m.get("question")
        tokens = m.get("clobTokenIds")
        if isinstance(tokens, str):
            try: tokens = json.loads(tokens)
            except Exception: tokens = []
        if not tokens or len(tokens) < 2:
            print(f"skip {slug}: no token ids", file=sys.stderr); continue
        yes_tok, no_tok = str(tokens[0]), str(tokens[1])
        neg_risk = bool(m.get("negRisk") or False)
        v = _safe_float(m.get("rewardsMaxSpread"))
        ms = _safe_float(m.get("rewardsMinSize"))
        rate = _safe_float(m.get("rewardsDailyRate")) or 0
        size = float(args.size) if args.size is not None else (ms or 100.0)
        if not v or v <= 0:
            print(f"skip {slug}: no rewards program (max_spread={v})", file=sys.stderr); continue
        if args.offset >= v:
            print(f"skip {slug}: offset {args.offset}¢ >= max_spread {v}¢ — would be ineligible",
                  file=sys.stderr); continue

        for side, token in (("yes", yes_tok), ("no", no_tok)):
            mid = midpoint(client, token) if client else None
            if mid is None:
                # Fall back to public CLOB midpoint (dry-run path).
                try:
                    r = requests.get(f"{CLOB_HOST}/midpoint",
                                     params={"token_id": token}, headers=HDRS, timeout=10)
                    mid = _safe_float((r.json() or {}).get("mid"))
                except Exception:
                    mid = None
            if mid is None:
                print(f"skip {slug} {side}: no midpoint", file=sys.stderr); continue
            tick = tick_for(client, token) if client else 0.01
            target = round_to_tick(mid - args.offset / 100.0, tick)
            rec = {"slug": slug, "side": side, "token_id": token,
                   "mid": mid, "target_price": target, "size": size,
                   "v_cents": v, "min_size": ms, "daily_rate": rate}
            print(f"{'PLACE' if args.live else 'DRY '} {slug:<60} {side.upper():3} "
                  f"@{target:.4f} x{size:g}  (mid={mid:.4f}, v={v}¢)")
            if args.live:
                from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
                from py_clob_client.order_builder.constants import BUY
                try:
                    order = client.create_order(
                        OrderArgs(token_id=token, price=target, size=size, side=BUY),
                        PartialCreateOrderOptions(neg_risk=neg_risk))
                    resp = client.post_order(order, OrderType.GTC, True)
                    rec["resp"] = resp
                except Exception as e:
                    rec["error"] = f"{type(e).__name__}: {e}"
                    print(f"  ERROR: {rec['error']}", file=sys.stderr)
                time.sleep(0.2)  # gentle pace; CLOB rate-limits aggressive bursts
            out_records.append(rec)

    if args.out:
        Path(args.out).write_text(json.dumps(out_records, indent=2, default=str))
        print(f"audit dump → {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
