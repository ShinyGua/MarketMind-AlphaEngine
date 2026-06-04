"""
Test that yfinance can fetch data for all configured horizons.
Run: python tests/test_yfinance_horizons.py
"""
import yfinance as yf
import sys

TICKER = "NVDA"
HORIZONS = {
    "short":  {"period": "5d",  "interval": "1h"},
    "medium": {"period": "3mo", "interval": "1d"},
    "long":   {"period": "2y",  "interval": "1mo"},
}

def _check_horizon(name, period, interval):
    print(f"\n--- {name}: period={period}, interval={interval} ---")
    try:
        df = yf.download(TICKER, period=period, interval=interval, progress=False)
        if df.empty:
            print(f"  FAIL: empty dataframe")
            return False
        print(f"  OK: {len(df)} rows, date range: {df.index[0]} to {df.index[-1]}")
        print(f"  Columns: {list(df.columns)}")
        close_col = df['Close']
        latest = close_col.iloc[-1] if close_col.ndim == 1 else close_col.iloc[-1].values[0]
        print(f"  Latest close: {float(latest):.2f}")
        return True
    except Exception as e:
        print(f"  FAIL: {e}")
        return False

if __name__ == "__main__":
    print(f"Testing yfinance horizons for {TICKER}")
    results = {}
    for name, params in HORIZONS.items():
        results[name] = _check_horizon(name, **params)

    print("\n=== SUMMARY ===")
    all_pass = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_pass = False

    sys.exit(0 if all_pass else 1)
