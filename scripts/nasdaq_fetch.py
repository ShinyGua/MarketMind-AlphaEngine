#!/usr/bin/env python3
"""Fetch NASDAQ news + quote for a US-listed symbol (deterministic plumbing).

Tries the public (unofficial) ``api.nasdaq.com`` JSON endpoints first with a
browser User-Agent. On ANY failure (403/timeout/empty/parse error) it prints
``{"fallback_needed": true, "reason": ...}`` so the caller (mm-web-research)
can fall back to WebSearch + WebFetch of nasdaq.com pages. Never raises.

Usage:
    .venv/bin/python3 scripts/nasdaq_fetch.py {TICKER} [--limit N]

Output (stdout, JSON):
  success:  {"provider":"nasdaq_api","ticker":...,"quote":{...},
             "articles":[{title,url,published_at,source,excerpt}],"count":N}
  fallback: {"fallback_needed": true, "reason": "..."}
"""
import argparse
import json
import sys

API_BASE = "https://api.nasdaq.com"
WWW = "https://www.nasdaq.com"

# api.nasdaq.com rejects non-browser agents; these headers are required.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": WWW,
    "Referer": WWW + "/",
}


def _abs_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("http"):
        return url
    return WWW + url if url.startswith("/") else f"{WWW}/{url}"


def fetch_quote(ticker: str, requests) -> dict:
    url = f"{API_BASE}/api/quote/{ticker}/info?assetclass=stocks"
    resp = requests.get(url, headers=HEADERS, timeout=12)
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or {}
    primary = data.get("primaryData") or {}
    return {
        "company_name": data.get("companyName"),
        "last_price": primary.get("lastSalePrice"),
        "net_change": primary.get("netChange"),
        "pct_change": primary.get("percentageChange"),
        "as_of": primary.get("lastTradeTimestamp"),
        "exchange": data.get("exchange") or data.get("stockType"),
    }


def fetch_news(ticker: str, limit: int, requests) -> list[dict]:
    url = (f"{API_BASE}/api/news/topic/articlebysymbol"
           f"?q={ticker}|stocks&offset=0&limit={limit}&fallback=true")
    resp = requests.get(url, headers=HEADERS, timeout=12)
    resp.raise_for_status()
    rows = ((resp.json() or {}).get("data") or {}).get("rows") or []
    articles = []
    for r in rows[:limit]:
        articles.append({
            "title": r.get("title"),
            "url": _abs_url(r.get("url", "")),
            "published_at": r.get("created") or r.get("ago"),
            "source": r.get("publisher") or "Nasdaq",
            "excerpt": (r.get("summary") or r.get("title") or "").strip(),
        })
    return [a for a in articles if a["title"]]


def fetch(ticker: str, limit: int) -> dict:
    try:
        import requests
    except Exception as e:  # pragma: no cover
        return {"fallback_needed": True, "reason": f"requests unavailable: {e}"}

    ticker = ticker.upper().strip()
    quote, articles, errors = None, [], []
    try:
        quote = fetch_quote(ticker, requests)
    except Exception as e:
        errors.append(f"quote: {type(e).__name__}")
    try:
        articles = fetch_news(ticker, limit, requests)
    except Exception as e:
        errors.append(f"news: {type(e).__name__}")

    # If we got nothing usable, signal fallback.
    if not articles and not (quote and quote.get("last_price")):
        return {"fallback_needed": True,
                "reason": "; ".join(errors) or "empty nasdaq response"}

    return {
        "provider": "nasdaq_api",
        "ticker": ticker,
        "quote": quote,
        "articles": articles,
        "count": len(articles),
        "errors": errors,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch NASDAQ news + quote for a US symbol.")
    ap.add_argument("ticker")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()
    print(json.dumps(fetch(args.ticker, args.limit), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
