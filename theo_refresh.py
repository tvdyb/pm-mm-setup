#!/usr/bin/env python3
"""Generate / refresh per-market theo files in `./theos/`.

Each file looks like:

    {
      "slug": "will-trump-fire-jerome-powell-by-end-of-2026",
      "theo_yes": 0.18,
      "as_of": "2026-04-30T22:01:11Z",
      "method": "Kalshi mirror price (KXTRUMPFIREPOWELL-26 YES bid 0.17 / ask 0.20)",
      "confidence": "high",
      "source": "kalshi-api"
    }

The dashboard loads everything in `theos/*.json`, joins on `slug`, and shows
the theo as a `theo_yes¢` column with edge vs. best bid.

This file is a *scaffold*. To add a new theo source: drop a
`refresh_<name>(slug=...)` function in here that returns the dict above (or
raises) and add it to the `REFRESHERS` dict at the bottom. Only one source
per slug — last one wins on collision.

A few reusable helpers are included:
  - `mirror_kalshi(market_ticker)` — pull a YES probability from a Kalshi
    market that mirrors a Polymarket binary.
  - `mirror_polymarket(slug)` — read another Polymarket market's mid (e.g.
    when a `_thru_year_end` market should anchor a `_thru_q3` market).
  - `model_normal_threshold(value, sigma, threshold)` — Normal CDF helper for
    threshold markets (e.g. "will index close above X by date Y").

Usage:
    python3 theo_refresh.py                # run every refresher, write theos/
    python3 theo_refresh.py --slug X       # only refresh one market
    python3 theo_refresh.py --dry-run      # print what would be written
"""
import argparse, datetime as dt, json, math, os, sys
from pathlib import Path
import requests

THEOS_DIR  = Path(os.environ.get("POLYMARKET_THEOS_DIR", "./theos"))
GAMMA_HOST = os.environ.get("POLYMARKET_GAMMA_HOST", "https://gamma-api.polymarket.com")
CLOB_HOST  = os.environ.get("POLYMARKET_CLOB_HOST",  "https://clob.polymarket.com")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"  # public, no auth needed for /markets
HDRS = {"User-Agent": "polymarket-mm-setup/1.0", "Accept": "application/json"}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_float(x):
    try: return float(x) if x is not None else None
    except (TypeError, ValueError): return None


# ---------------------------------------------------------------------------
# Helpers callable by the per-slug refreshers.
# ---------------------------------------------------------------------------

def mirror_polymarket(slug):
    """Read another Polymarket market's mid as a probability. Useful when one
    market is a strict subset of another (e.g. 'by Q3' implies 'by year end')."""
    r = requests.get(f"{GAMMA_HOST}/markets", params={"slug": slug},
                     headers=HDRS, timeout=10)
    r.raise_for_status()
    arr = r.json() or []
    if not arr:
        raise RuntimeError(f"polymarket: no market with slug {slug!r}")
    m = arr[0]
    bid = safe_float(m.get("bestBid"))
    ask = safe_float(m.get("bestAsk"))
    if bid is not None and ask is not None and 0 < bid <= ask < 1:
        return 0.5 * (bid + ask)
    op = m.get("outcomePrices")
    if isinstance(op, str):
        try: op = json.loads(op)
        except Exception: op = []
    if isinstance(op, list) and op:
        return safe_float(op[0])
    last = safe_float(m.get("lastTradePrice"))
    if last is not None: return last
    raise RuntimeError(f"polymarket: no usable price for slug {slug!r}")


def mirror_kalshi(market_ticker):
    """Fetch a Kalshi market's mid as a YES probability. Public endpoint; no
    API key required for /markets/{ticker}."""
    r = requests.get(f"{KALSHI_BASE}/markets/{market_ticker}", headers=HDRS, timeout=10)
    r.raise_for_status()
    m = (r.json() or {}).get("market") or {}
    yb = safe_float(m.get("yes_bid"))
    ya = safe_float(m.get("yes_ask"))
    if yb is not None and ya is not None and 0 <= yb <= ya <= 100:
        return 0.5 * (yb + ya) / 100.0
    last = safe_float(m.get("last_price"))
    if last is not None: return last / 100.0
    raise RuntimeError(f"kalshi: no usable price for {market_ticker!r}")


def model_normal_threshold(current, sigma, threshold, days_remaining,
                           direction="above"):
    """Probability that a Brownian-like process starting at `current` with
    daily-vol `sigma` exceeds (or falls below) `threshold` on the resolution
    date. Useful for index / commodity threshold markets.

    direction="above" → P(value at T >= threshold)
    """
    if days_remaining <= 0:
        return 1.0 if (direction == "above" and current >= threshold) else 0.0
    sd = sigma * math.sqrt(days_remaining)
    if sd <= 0: return 1.0 if (direction == "above" and current >= threshold) else 0.0
    z = (threshold - current) / sd
    p_above = 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))
    return p_above if direction == "above" else (1.0 - p_above)


# ---------------------------------------------------------------------------
# Per-market refreshers. Add yours below and register in REFRESHERS.
# ---------------------------------------------------------------------------

def example_refresher(slug):
    """Skeleton: replace the body with whatever produces a YES probability
    for this market, then register it in REFRESHERS. The wrapping logic
    (file write, error swallowing, dry-run) is handled by main()."""
    raise NotImplementedError("define a real source for this market")


# Map of market slug → callable returning {theo_yes, method, confidence, source}
# (or any superset; the helpers below normalize to a complete record).
REFRESHERS = {
    # "will-fed-cut-rates-in-may-2026": lambda slug: {
    #     "theo_yes": mirror_kalshi("KXFEDDECISION-26MAY-CUT"),
    #     "method": "Kalshi mirror KXFEDDECISION-26MAY-CUT",
    #     "confidence": "high",
    #     "source": "kalshi",
    # },
}


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

def write_theo(slug, payload, dry_run=False):
    rec = {
        "slug": slug,
        "theo_yes": float(payload["theo_yes"]),
        "as_of": now_iso(),
        "method": payload.get("method") or "",
        "confidence": payload.get("confidence") or "",
        "source": payload.get("source") or "",
    }
    p = THEOS_DIR / f"{slug}.json"
    if dry_run:
        print(f"DRY-WRITE {p}: {json.dumps(rec)}")
        return
    THEOS_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, indent=2))
    print(f"wrote {p} (theo_yes={rec['theo_yes']:.4f})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", help="run only this slug's refresher")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not REFRESHERS:
        print("note: REFRESHERS dict is empty — add per-slug entries to start "
              "producing theos.", file=sys.stderr)

    targets = ([args.slug] if args.slug else list(REFRESHERS.keys()))
    errors = []
    for slug in targets:
        fn = REFRESHERS.get(slug)
        if fn is None:
            errors.append({"slug": slug, "error": "no refresher registered"})
            continue
        try:
            payload = fn(slug)
            if "theo_yes" not in payload:
                raise RuntimeError("refresher returned no theo_yes")
            write_theo(slug, payload, dry_run=args.dry_run)
        except Exception as e:
            errors.append({"slug": slug, "error": f"{type(e).__name__}: {e}"})
            print(f"ERR {slug}: {e}", file=sys.stderr)
    if errors:
        sys.exit(1 if args.slug else 0)


if __name__ == "__main__":
    main()
