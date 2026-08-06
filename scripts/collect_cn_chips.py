#!/usr/bin/env python3
"""A-share chip/flow collection via akshare for MarketMind-AlphaEngine.

Collects the signals that answer "筹码干不干净" — the ones yfinance cannot see:

| block            | akshare interface                     | question answered        |
|------------------|---------------------------------------|--------------------------|
| turnover         | stock_zh_a_hist (换手率 column)        | chip-exchange intensity  |
| fund_flow        | stock_individual_fund_flow            | who is buying (主力大单) |
| lhb              | stock_lhb_detail_em (filtered)        | hot-money vs institutions|
| northbound       | stock_hsgt_individual_em              | long-money attitude      |
| margin           | stock_margin_detail_szse/sse          | leveraged chips          |
| holder_count     | stock_zh_a_gdhs_detail_em             | concentration/dispersion |
| restricted       | stock_restricted_release_queue_em     | unlock overhang (解禁)   |

Writes {workspace}/raw/{date}/chips/cn_flows.json. Each block carries its own
`data_quality` ("akshare" | "unavailable") and degrades independently — a
blocked endpoint (e.g. push2his fund-flow behind some networks) never takes the
others down. scripts/compute_chip_structure.py embeds the file under
`cn_flows` in chip_structure.json.

Scope: `market_profile == "CN"` only. HK/US names exit 0 without writing —
their chip read comes from the OHLCV-based engine alone. akshare missing or
import failure → exit 0 with a stub file, never fails the pipeline.

Network note: akshare inherits requests' proxy env. On the first connection
failure the collector strips proxy vars and retries — CN data hosts are
usually direct-reachable while generic proxies often break them.

Usage:
    .venv/bin/python3 scripts/collect_cn_chips.py <workspace> <date>
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
from shared import contracts  # noqa: E402

_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "all_proxy")
_proxy_stripped = False


def _num(x):
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return None


def _call(fn):
    """Call an akshare fetcher; on a connection-level failure, strip proxy env
    once (process-wide) and retry. Raises the final failure to the caller."""
    global _proxy_stripped
    try:
        return fn()
    except Exception:
        if _proxy_stripped:
            raise
        _proxy_stripped = True
        for k in _PROXY_KEYS:
            os.environ.pop(k, None)
        return fn()


def _block(builder, attempts: int = 2):
    """Run one signal builder; any failure becomes an unavailable block.
    One paced retry — eastmoney hosts drop connections intermittently."""
    import time

    last = None
    for i in range(attempts):
        try:
            data = builder()
            data.setdefault("data_quality", "akshare")
            return data
        except Exception as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(1.5)
    return {"data_quality": "unavailable",
            "reason": f"{type(last).__name__}: {str(last)[:120]}"}


def _yyyymmdd(date: str) -> str:
    return date.replace("-", "")


def _back_days(date: str, days: int) -> str:
    d = _dt.date.fromisoformat(date) - _dt.timedelta(days=days)
    return d.strftime("%Y%m%d")


# ── signal builders ──────────────────────────────────────────────────

def build_turnover(ak, code: str, date: str):
    df = _call(lambda: ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=_back_days(date, 60), end_date=_yyyymmdd(date)))
    if df is None or df.empty or "换手率" not in df.columns:
        raise ValueError("no turnover column")
    turn = df["换手率"].astype(float)
    return {
        "turnover_pct": _num(turn.iloc[-1]),
        "avg_5d_pct": _num(turn.tail(5).mean()),
        "avg_20d_pct": _num(turn.tail(20).mean()) if len(turn) >= 20 else None,
        "series_tail": [_num(v) for v in turn.tail(10)],
        "source": "stock_zh_a_hist.turnover_rate", "source_field_cn": "换手率",
    }


def build_fund_flow(ak, code: str, market: str, date: str):
    df = _call(lambda: ak.stock_individual_fund_flow(stock=code, market=market))
    if df is None or df.empty:
        raise ValueError("empty fund flow")
    df = df[df["日期"].astype(str) <= date]
    if df.empty:
        raise ValueError("no rows on/before run date")
    col = "主力净流入-净额"
    if col not in df.columns:
        raise ValueError("main-force column missing")
    net = df[col].astype(float)

    def _consecutive(sign):
        n = 0
        for v in net.iloc[::-1]:
            if (v > 0) == sign and v != 0:
                n += 1
            else:
                break
        return n

    return {
        "net_1d": _num(net.iloc[-1]),
        "net_5d": _num(net.tail(5).sum()),
        "net_10d": _num(net.tail(10).sum()),
        "net_20d": _num(net.tail(20).sum()) if len(net) >= 20 else None,
        "consecutive_inflow_days": _consecutive(True),
        "consecutive_outflow_days": _consecutive(False),
        "source": "stock_individual_fund_flow.main_net_inflow", "source_field_cn": "主力净流入",
    }


def build_lhb(ak, code: str, date: str):
    df = _call(lambda: ak.stock_lhb_detail_em(
        start_date=_back_days(date, 90), end_date=_yyyymmdd(date)))
    if df is None or df.empty:
        raise ValueError("empty LHB window")
    rows = df[df["代码"].astype(str).str.zfill(6) == code.zfill(6)]
    events = []
    for _, r in rows.tail(5).iterrows():
        events.append({
            "date": str(r.get("上榜日", ""))[:10],
            "reason": str(r.get("上榜原因", ""))[:60],
            "interpretation": str(r.get("解读", ""))[:60],
            "net_buy": _num(r.get("龙虎榜净买额")),
        })
    return {"appearances_90d": int(len(rows)), "events": events,
            "source": "stock_lhb_detail_em"}


def build_northbound(ak, code: str, date: str):
    df = _call(lambda: ak.stock_hsgt_individual_em(symbol=code))
    if df is None or df.empty:
        raise ValueError("empty northbound history")
    df = df[df["持股日期"].astype(str) <= date]
    if df.empty:
        raise ValueError("no rows on/before run date")
    df = df.sort_values("持股日期")
    pct = df["持股数量占A股百分比"].astype(float)
    out = {
        "holding_pct": _num(pct.iloc[-1]),
        "holding_pct_5d_ago": _num(pct.iloc[-6]) if len(pct) >= 6 else None,
        "holding_pct_20d_ago": _num(pct.iloc[-21]) if len(pct) >= 21 else None,
        "source": "stock_hsgt_individual_em",
    }
    if out["holding_pct"] is not None and out["holding_pct_5d_ago"] is not None:
        out["change_5d_pp"] = round(out["holding_pct"] - out["holding_pct_5d_ago"], 3)
    return out


def build_margin(ak, code: str, exchange_hint: str, date: str):
    is_sse = code.startswith("6") or "SSE" in exchange_hint.upper()
    fetcher = (lambda d: ak.stock_margin_detail_sse(date=d)) if is_sse \
        else (lambda d: ak.stock_margin_detail_szse(date=d))
    last_err = None
    for back in range(0, 8):
        day = _back_days(date, back)
        try:
            df = _call(lambda d=day: fetcher(d))
        except Exception as exc:
            last_err = exc
            continue
        if df is None or df.empty:
            continue
        code_col = next((c for c in df.columns if "代码" in str(c)), None)
        bal_col = next((c for c in df.columns if "融资余额" in str(c)), None)
        if not code_col or not bal_col:
            continue
        row = df[df[code_col].astype(str).str.zfill(6) == code.zfill(6)]
        if row.empty:
            raise ValueError("not a margin-eligible name")
        return {"as_of": day, "margin_balance": _num(row.iloc[0][bal_col]),
                "source": "stock_margin_detail_sse/szse"}
    raise last_err or ValueError("no margin data within 7 days")


def build_holder_count(ak, code: str, date: str):
    df = _call(lambda: ak.stock_zh_a_gdhs_detail_em(symbol=code))
    if df is None or df.empty:
        raise ValueError("empty holder-count history")
    date_col = next((c for c in df.columns if "股东户数统计截止日" in str(c) or "截止日" in str(c)), None)
    cur_col = next((c for c in df.columns if "本次" in str(c) and "户数" in str(c)), None)
    chg_col = next((c for c in df.columns if "增减比例" in str(c)), None)
    if not (date_col and cur_col):
        raise ValueError(f"unexpected columns: {list(df.columns)[:6]}")
    df = df[df[date_col].astype(str) <= date].sort_values(date_col)
    if df.empty:
        raise ValueError("no rows on/before run date")
    recent = df.tail(4)
    history = [{"as_of": str(r[date_col])[:10], "holders": _num(r[cur_col]),
                "change_pct": _num(r[chg_col]) if chg_col else None}
               for _, r in recent.iterrows()]
    changes = [h["change_pct"] for h in history if h["change_pct"] is not None]
    trend = "concentrating" if changes and all(c < 0 for c in changes[-2:]) else \
            "dispersing" if changes and all(c > 0 for c in changes[-2:]) else "mixed"
    return {"latest": history[-1] if history else None, "history": history,
            "trend": trend, "source": "stock_zh_a_gdhs_detail_em"}


def build_restricted(ak, code: str, date: str):
    df = _call(lambda: ak.stock_restricted_release_queue_em(symbol=code))
    if df is None or df.empty:
        return {"upcoming": [], "note": "no restricted-release queue",
                "source": "stock_restricted_release_queue_em"}
    date_col = next((c for c in df.columns if "解禁时间" in str(c)), None)
    if not date_col:
        raise ValueError("no release-date column")
    upcoming = []
    for _, r in df.iterrows():
        day = str(r[date_col])[:10]
        if day >= date:
            upcoming.append({
                "date": day,
                "shares": _num(r.get("解禁数量")),
                "pct_of_total": _num(r.get("占总市值比例") or r.get("占解禁前流通市值比例")),
            })
    upcoming.sort(key=lambda u: u["date"])
    return {"upcoming": upcoming[:4], "source": "stock_restricted_release_queue_em"}


# ── driver ───────────────────────────────────────────────────────────

def run(workspace: Path, date: str) -> dict | None:
    profile = {}
    try:
        profile = json.loads((workspace / "profile" / "company_profile.json")
                             .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    market = (profile.get("market_profile") or "US").upper()
    if market != "CN":
        print(f"cn_chips: skip (market_profile={market}; A-share interfaces only)")
        return None

    out_path = workspace / "raw" / date / "chips" / "cn_flows.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    code = Path(str(workspace)).name
    # bare 6-digit A-share code from yf symbol if the dir name isn't one
    if not (code.isdigit() and len(code) == 6):
        sym = contracts.resolve_yf_symbol(workspace)
        code = sym.split(".")[0]
    exchange = str(profile.get("exchange") or "")
    em_market = "sh" if (code.startswith("6") or "SSE" in exchange.upper()) else "sz"

    try:
        import akshare as ak
    except ImportError:
        artifact = {"available": False, "reason": "akshare_not_installed",
                    "date": date, "code": code}
        out_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        print(f"cn_chips: akshare not installed — wrote stub {out_path}")
        return artifact

    artifact = {
        "available": True,
        "date": date,
        "code": code,
        "turnover": _block(lambda: build_turnover(ak, code, date)),
        "main_force": _block(lambda: build_fund_flow(ak, code, em_market, date)),
        "lhb": _block(lambda: build_lhb(ak, code, date)),
        "northbound": _block(lambda: build_northbound(ak, code, date)),
        "margin": _block(lambda: build_margin(ak, code, exchange, date)),
        "holder_count": _block(lambda: build_holder_count(ak, code, date)),
        "restricted_release": _block(lambda: build_restricted(ak, code, date)),
    }
    ok = [k for k, v in artifact.items()
          if isinstance(v, dict) and v.get("data_quality") == "akshare"]
    bad = [k for k, v in artifact.items()
           if isinstance(v, dict) and v.get("data_quality") == "unavailable"]
    artifact["blocks_ok"] = ok
    artifact["blocks_unavailable"] = bad
    out_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"cn_chips: wrote {out_path} (ok={ok}, unavailable={bad})")
    return artifact


def main():
    if len(sys.argv) < 3:
        print("usage: collect_cn_chips.py <workspace> <date>")
        return
    try:
        run(Path(sys.argv[1]), sys.argv[2])
    except Exception as exc:  # advisory stage — never fail the pipeline
        print(f"cn_chips: ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
