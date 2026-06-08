"""
MarketMind Chart Generator v3 — Annotated Institutional Style
Generates PDF charts for equity research reports.

Usage:
    .venv/bin/python3 templates/charts.py <workspace> <date> <ticker> [catalysts_json]

Outputs charts to: {workspace}/exports/{date}/pdf/charts/
"""

import sys
import json
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates  # kept for potential future use
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta

# ============================================================
# Institutional Chart Style
# ============================================================
COLORS = {
    'primary': '#1B2A4A',
    'secondary': '#2B5EA7',
    'accent': '#3B82F6',
    'positive': '#16A34A',
    'negative': '#DC2626',
    'gray': '#9CA3AF',
    'light_gray': '#E5E7EB',
    'very_light': '#F3F4F6',
    'bg': '#FFFFFF',
    'text': '#2D2D2D',
    'muted': '#6B7280',
    'highlight': '#FEF3C7',  # Amber highlight
}

# ============================================================
# Bilingual Labels
# ============================================================
LABELS = {
    'en': {
        'price_action': 'Price Action',
        'relative_perf': 'Relative Performance',
        'peer_perf': '5-Day Performance',
        'vol': 'Vol',
        'macd': 'MACD',
        'upcoming': 'Upcoming',
        'return_pct': 'Return (%)',
        '5d_return': '5-Day Return (%)',
        'golden_cross': 'Golden Cross',
        'death_cross': 'Death Cross',
        'rank': '#{rank} of {total}',
    },
    'ch': {
        'price_action': '股价走势',
        'relative_perf': '相对表现',
        'peer_perf': '5日表现',
        'vol': '成交量',
        'macd': 'MACD',
        'upcoming': '即将',
        'return_pct': '收益率 (%)',
        '5d_return': '5日收益率 (%)',
        'golden_cross': '金叉',
        'death_cross': '死叉',
        'rank': '第{rank}名/共{total}只',
    },
}

# Global language setting (set by main() from CLI arg or config)
LANG = 'en'

def L(key):
    """Get localized label."""
    return LABELS.get(LANG, LABELS['en']).get(key, key)


def _setup_rcparams():
    """Set matplotlib defaults, including CJK fonts for Chinese."""
    fonts = ['Helvetica Neue', 'Helvetica', 'Arial', 'DejaVu Sans', 'sans-serif']
    if LANG == 'ch':
        # Order by likely availability across OSes; WenQuanYi/Noto are the
        # common Linux CJK faces (WenQuanYi is what ships in this environment).
        fonts = ['PingFang SC', 'Heiti SC', 'Microsoft YaHei',
                 'Noto Sans CJK SC', 'Source Han Sans SC',
                 'WenQuanYi Micro Hei', 'WenQuanYi Zen Hei'] + fonts
    plt.rcParams.update({
        'font.family': 'sans-serif',
        'font.sans-serif': fonts,
        'axes.unicode_minus': False,
        'font.size': 9,
        'axes.labelcolor': COLORS['text'],
        'axes.edgecolor': COLORS['light_gray'],
        'xtick.color': COLORS['muted'],
        'ytick.color': COLORS['muted'],
        'figure.facecolor': COLORS['bg'],
        'axes.facecolor': COLORS['bg'],
        'axes.grid': True,
        'grid.alpha': 0.2,
        'grid.color': COLORS['light_gray'],
        'axes.spines.top': False,
        'axes.spines.right': False,
    })


def load_price_csv(path):
    """Load a yfinance CSV, handling both single-header and MultiIndex columns."""
    # Sniff how many header rows there are
    with open(path) as _f:
        first_line = _f.readline().strip()
        second_line = _f.readline().strip()

    # If the second row starts with 'Ticker' or a ticker symbol repeated, it's MultiIndex
    second_cells = second_line.split(',')
    # Heuristic: MultiIndex yfinance files have a 'Ticker' or non-date token in row 2 col 0
    is_multi = second_cells[0].strip().lower() in ('ticker', 'price') or (
        len(second_cells) > 1 and not second_cells[0].strip().replace('-', '').replace('/', '').replace(' ', '').isdigit()
        and second_cells[0].strip() not in ('', 'Date')
    )

    if is_multi:
        df = pd.read_csv(path, header=[0, 1], index_col=0, parse_dates=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        # Drop the 'Ticker'/'Date' meta rows that yfinance injects
        df = df.loc[~df.index.astype(str).str.lower().isin(['ticker', 'date', 'price', ''])]
    else:
        df = pd.read_csv(path, index_col=0, parse_dates=False)

    df = df.loc[:, ~df.columns.duplicated()]
    # Normalize column names to title-case so lowercase CSVs work (e.g. 'close' -> 'Close')
    df.columns = [c.strip().title() if c.strip().lower() in ('close', 'open', 'high', 'low', 'volume', 'date') else c for c in df.columns]
    for col in ['Close', 'Open', 'High', 'Low', 'Volume']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    # Ensure the index is tz-naive date-normalized datetime for consistent arithmetic
    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.index = pd.to_datetime(df.index, utc=True, errors='coerce')
        except Exception:
            df.index = pd.to_datetime(df.index, errors='coerce')
    if hasattr(df.index, 'tz') and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    # Normalize to midnight so NVDA (+04:00) and SPY (00:00) align
    df.index = df.index.normalize()
    df = df[df.index.notna()]
    return df.dropna(subset=['Close'])


def find_sma_crossovers(sma_short, sma_long):
    """Find golden cross and death cross points."""
    crossovers = []
    if sma_short is None or sma_long is None:
        return crossovers
    for i in range(1, len(sma_short)):
        if pd.isna(sma_short.iloc[i]) or pd.isna(sma_long.iloc[i]):
            continue
        if pd.isna(sma_short.iloc[i-1]) or pd.isna(sma_long.iloc[i-1]):
            continue
        if sma_short.iloc[i] > sma_long.iloc[i] and sma_short.iloc[i-1] <= sma_long.iloc[i-1]:
            crossovers.append(('golden', sma_short.index[i], sma_short.iloc[i]))
        elif sma_short.iloc[i] < sma_long.iloc[i] and sma_short.iloc[i-1] >= sma_long.iloc[i-1]:
            crossovers.append(('death', sma_short.index[i], sma_short.iloc[i]))
    return crossovers


def _date_to_idx(dates, target):
    """Find nearest integer index for a target date in a DatetimeIndex."""
    diffs = abs(dates - target)
    return int(diffs.argmin())


def _make_tick_labels(dates, n_ticks=8):
    """Generate evenly spaced tick positions and date labels."""
    n = len(dates)
    positions = np.linspace(0, n - 1, min(n_ticks, n), dtype=int)
    labels = [dates[i].strftime('%b %d') for i in positions]
    return positions, labels


# Display window (≈3 months of trading days) and the minimum raw rows needed for
# a full SMA50 at the first visible bar (65 display + 49 prior bars = 114).
DISPLAY_WINDOW = 65
WARMUP_MIN_ROWS = 114


def _prepare_chart_df(df, window=DISPLAY_WINDOW):
    """Compute SMA20/SMA50 and MACD(12,26,9) on the FULL dataframe, then slice to
    the last `window` rows. Computing before trimming is what makes the overlays
    valid across the entire visible window (warm-up preserved). Pure: no I/O."""
    df = df.copy()
    close = df['Close']
    df['SMA20'] = close.rolling(20).mean()
    df['SMA50'] = close.rolling(50).mean()
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['MACD_signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['MACD_hist'] = df['MACD'] - df['MACD_signal']
    if window and len(df) > window:
        df = df.iloc[-window:]
    return df


def _fetch_warmup_history(ticker, run_date=None):
    """Best-effort in-memory 6mo daily OHLCV for indicator warm-up.

    Rendering stays read-only and never network-dependent: this returns a
    DataFrame shaped like load_price_csv (date-bounded to run_date so the latest
    bar/price matches the rest of the report), or None on ANY failure. It never
    raises and never writes to disk."""
    try:
        import yfinance as yf
        raw = yf.Ticker(ticker).history(period='6mo', interval='1d')
        if raw is None or raw.empty:
            return None
        cols = [c for c in ['Open', 'High', 'Low', 'Close', 'Volume'] if c in raw.columns]
        df = raw[cols].copy()
        if hasattr(df.index, 'tz') and df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df.index = pd.to_datetime(df.index).normalize()
        if run_date:
            df = df[df.index <= pd.Timestamp(run_date).normalize()]
        df = df.dropna(subset=['Close'])
        return df if not df.empty else None
    except Exception as e:  # network unavailable / fetch error → caller degrades
        print(f'  [warn] warm-up fetch failed for {ticker}: {e}')
        return None


def generate_price_chart(ticker, price_path, output_path, catalysts=None, run_date=None):
    """Annotated price chart with SMA, MACD, crossovers, events, latest price.
    Uses integer x-axis to eliminate weekend/holiday gaps."""
    df = load_price_csv(price_path)
    if df.empty:
        return

    # Warm-up: if the raw file is too short for a full SMA50 across the visible
    # window, try an in-memory 6mo fetch (read-only — never overwrites the raw CSV;
    # degrades to partial overlays if no network).
    if len(df) < WARMUP_MIN_ROWS:
        warm = _fetch_warmup_history(ticker, run_date)
        if warm is not None and len(warm) > len(df):
            df = warm

    # Compute indicators on the full df, THEN trim to the display window.
    df = _prepare_chart_df(df, window=DISPLAY_WINDOW)

    dates = df.index
    x = np.arange(len(df))

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(8, 6.5),
                                        gridspec_kw={'height_ratios': [3.2, 1, 1.1]},
                                        sharex=True)
    fig.subplots_adjust(hspace=0.08)

    # Price line (thicker)
    ax1.plot(x, df['Close'].values, color=COLORS['primary'], linewidth=2, label=ticker)

    # SMA overlays (precomputed on the full df → valid across the whole window)
    sma20 = df['SMA20'] if df['SMA20'].notna().any() else None
    sma50 = df['SMA50'] if df['SMA50'].notna().any() else None
    if sma20 is not None:
        ax1.plot(x, sma20.values, color=COLORS['accent'], linewidth=1,
                 linestyle='--', alpha=0.6, label='SMA 20')
    if sma50 is not None:
        ax1.plot(x, sma50.values, color=COLORS['gray'], linewidth=1,
                 linestyle='--', alpha=0.6, label='SMA 50')

    # === ANNOTATION: SMA Crossovers ===
    crossovers = find_sma_crossovers(sma20, sma50)
    for ctype, cdate, cprice in crossovers:
        color = COLORS['positive'] if ctype == 'golden' else COLORS['negative']
        label = L('golden_cross') if ctype == 'golden' else L('death_cross')
        xi = _date_to_idx(dates, cdate)
        ax1.scatter([xi], [cprice], color=color, s=80, zorder=5, edgecolors='white', linewidth=1.5)
        ax1.annotate(label, (xi, cprice),
                     textcoords="offset points", xytext=(8, 12),
                     fontsize=7, fontweight='bold', color=color,
                     arrowprops=dict(arrowstyle='->', color=color, lw=0.8))

    # Headroom at the top so the latest-price label and upcoming block don't
    # collide with the price line or title.
    ymin, ymax = ax1.get_ylim()
    ax1.set_ylim(ymin, ymax + (ymax - ymin) * 0.12)

    # === ANNOTATION: Latest price dot + label (anchored inward at the right edge) ===
    latest_price = df['Close'].iloc[-1]
    ax1.scatter([x[-1]], [latest_price], color=COLORS['primary'], s=60, zorder=5,
                edgecolors='white', linewidth=1.5)
    ax1.annotate(f'${latest_price:.2f}', (x[-1], latest_price),
                 textcoords="offset points", xytext=(-10, 8), ha='right',
                 fontsize=9, fontweight='bold', color=COLORS['primary'],
                 bbox=dict(boxstyle='round,pad=0.3', facecolor=COLORS['very_light'],
                          edgecolor=COLORS['light_gray'], alpha=0.9))

    # === ANNOTATION: Recent 5-day shading ===
    if len(df) >= 5:
        ax1.axvspan(x[-5], x[-1], alpha=0.06, color=COLORS['accent'])

    # === ANNOTATION: Catalyst events ===
    # In-window catalysts get a real vline at their true position; future ones are
    # collected into a single bounded, staggered "Upcoming" block (no longer pinned
    # to the same right-edge spot, which caused the overlap).
    if catalysts:
        upcoming = []
        for cat in catalysts:
            try:
                cat_date = pd.Timestamp(cat['date'])
            except (ValueError, KeyError, TypeError):
                continue
            event = (cat.get('event') or '')[:18]
            if dates[0] <= cat_date <= dates[-1]:
                xi = _date_to_idx(dates, cat_date)
                ax1.axvline(x=xi, color=COLORS['secondary'], linestyle=':',
                            alpha=0.45, linewidth=0.8)
                ax1.annotate(event, (xi, df['Close'].iloc[xi]),
                             textcoords="offset points", xytext=(3, 10),
                             fontsize=6, color=COLORS['secondary'], va='bottom', ha='left')
            elif cat_date > dates[-1]:
                upcoming.append((cat_date, event))
        upcoming = sorted(upcoming)[:3]
        if upcoming:
            block = [f"{L('upcoming')}:"] + [f"▸ {d.strftime('%b %d')}  {ev}" for d, ev in upcoming]
            ax1.text(0.99, 0.92, '\n'.join(block), transform=ax1.transAxes,
                     ha='right', va='top', fontsize=6, color=COLORS['secondary'],
                     bbox=dict(boxstyle='round,pad=0.3', facecolor=COLORS['very_light'],
                               edgecolor=COLORS['light_gray'], alpha=0.85))

    ax1.set_ylabel('', fontsize=8)
    ax1.legend(loc='upper left', fontsize=7, frameon=False)
    ax1.set_title(f'{ticker} — {L("price_action")}', fontsize=12, fontweight='bold',
                  color=COLORS['primary'], loc='left', pad=12)
    ax1.tick_params(labelbottom=False)

    # Volume bars
    if 'Volume' in df.columns:
        vol_colors = [COLORS['positive'] if df['Close'].iloc[i] >= df['Open'].iloc[i]
                      else COLORS['negative'] for i in range(len(df))]
        ax2.bar(x, df['Volume'].values, color=vol_colors, alpha=0.4, width=0.8)
        ax2.set_ylabel(L('vol'), fontsize=7, color=COLORS['muted'])
        ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, p: f'{v/1e6:.0f}M'))
        vol_avg = df['Volume'].rolling(20).mean()
        ax2.plot(x, vol_avg.values, color=COLORS['muted'], linewidth=0.7, alpha=0.5)
    ax2.tick_params(labelbottom=False)

    # MACD panel (below volume): histogram + MACD line + signal line
    macd, signal, hist = df['MACD'], df['MACD_signal'], df['MACD_hist']
    if macd.notna().any():
        hist_colors = [COLORS['positive'] if (h >= 0) else COLORS['negative']
                       for h in np.nan_to_num(hist.values)]
        ax3.bar(x, hist.values, color=hist_colors, alpha=0.35, width=0.8)
        ax3.plot(x, macd.values, color=COLORS['secondary'], linewidth=1, label=L('macd'))
        ax3.plot(x, signal.values, color=COLORS['accent'], linewidth=1,
                 linestyle='--', label='Signal')
        ax3.axhline(0, color=COLORS['light_gray'], linewidth=0.6)
        ax3.set_ylabel(L('macd'), fontsize=7, color=COLORS['muted'])
        ax3.legend(loc='upper left', fontsize=6, frameon=False, ncol=2)

    # X-axis: trading day labels only (no gaps), on the bottom MACD panel
    tick_pos, tick_labels = _make_tick_labels(dates)
    ax3.set_xticks(tick_pos)
    ax3.set_xticklabels(tick_labels, fontsize=7)

    plt.tight_layout()
    fig.savefig(output_path, bbox_inches='tight', dpi=150)  # format inferred from extension (.svg)
    plt.close(fig)
    print(f'  Generated: {output_path}')


def generate_relative_chart(ticker, ticker_path, spy_path, output_path, index_name='SPY'):
    """Annotated relative performance chart with spread labels.
    Uses integer x-axis to eliminate weekend/holiday gaps."""
    df_ticker = load_price_csv(ticker_path)
    df_spy = load_price_csv(spy_path)

    if df_ticker.empty or df_spy.empty:
        return

    common = df_ticker.index.intersection(df_spy.index)
    if len(common) < 5:
        return

    # Trim to last 3 months
    if len(common) > 65:
        common = common[-65:]

    t_close = df_ticker.loc[common, 'Close']
    s_close = df_spy.loc[common, 'Close']
    t_norm = (t_close / t_close.iloc[0] - 1) * 100
    s_norm = (s_close / s_close.iloc[0] - 1) * 100

    x = np.arange(len(common))

    fig, ax = plt.subplots(figsize=(8, 3.5))

    ax.plot(x, t_norm.values, color=COLORS['primary'], linewidth=2, label=ticker)
    ax.plot(x, s_norm.values, color=COLORS['gray'], linewidth=1.5, label=index_name, alpha=0.6)
    ax.fill_between(x, t_norm.values, s_norm.values,
                     where=(t_norm.values >= s_norm.values), alpha=0.08, color=COLORS['positive'])
    ax.fill_between(x, t_norm.values, s_norm.values,
                     where=(t_norm.values < s_norm.values), alpha=0.08, color=COLORS['negative'])

    ax.axhline(y=0, color=COLORS['light_gray'], linewidth=0.8)

    # === ANNOTATION: End-of-line spread labels ===
    t_final = float(t_norm.iloc[-1])
    s_final = float(s_norm.iloc[-1])
    spread = t_final - s_final

    ax.annotate(f'{ticker} {t_final:+.1f}%', (x[-1], t_final),
                textcoords="offset points", xytext=(8, 0),
                fontsize=8, fontweight='bold', color=COLORS['primary'])
    ax.annotate(f'{index_name} {s_final:+.1f}%', (x[-1], s_final),
                textcoords="offset points", xytext=(8, 0),
                fontsize=8, color=COLORS['gray'])

    # Alpha label
    spread_color = COLORS['positive'] if spread > 0 else COLORS['negative']
    ax.annotate(f'Alpha: {spread:+.1f}pp', xy=(0.98, 0.05),
                xycoords='axes fraction', fontsize=9, fontweight='bold',
                color=spread_color, ha='right',
                bbox=dict(boxstyle='round,pad=0.4', facecolor=spread_color,
                         alpha=0.1, edgecolor='none'))

    ax.set_ylabel(L('return_pct'), fontsize=8, color=COLORS['muted'])
    ax.set_title(f'{ticker} vs {index_name} — {L("relative_perf")}', fontsize=12,
                 fontweight='bold', color=COLORS['primary'], loc='left', pad=12)
    ax.legend(loc='upper left', fontsize=8, frameon=False)

    # X-axis: trading day labels only (no gaps)
    tick_pos, tick_labels = _make_tick_labels(common)
    ax.set_xticks(tick_pos)
    ax.set_xticklabels(tick_labels, fontsize=7)

    plt.tight_layout()
    fig.savefig(output_path, bbox_inches='tight', dpi=150)  # format inferred from extension (.svg)
    plt.close(fig)
    print(f'  Generated: {output_path}')


def generate_peer_chart(ticker, peer_data, output_path):
    """Peer bar chart with target ticker highlighted."""
    if not peer_data:
        return

    names = [p['name'] for p in peer_data]
    returns = [p['return_5d'] for p in peer_data]

    # Highlight target ticker in navy, others in gray/green/red
    colors = []
    for p, r in zip(peer_data, returns):
        if p['name'] == ticker:
            colors.append(COLORS['primary'])
        elif r >= 0:
            colors.append(COLORS['positive'] + '80')  # semi-transparent
        else:
            colors.append(COLORS['negative'] + '80')

    fig, ax = plt.subplots(figsize=(8, max(2.5, len(peer_data) * 0.5 + 1)))

    bars = ax.barh(names, returns, color=colors, height=0.5, alpha=0.9,
                   edgecolor='white', linewidth=0.5)

    # Value labels
    for i, (bar, val) in enumerate(zip(bars, returns)):
        x = bar.get_width()
        weight = 'bold' if peer_data[i]['name'] == ticker else 'normal'
        ax.text(x + (0.3 if x >= 0 else -0.3), bar.get_y() + bar.get_height()/2,
                f'{val:+.1f}%', va='center', ha='left' if x >= 0 else 'right',
                fontsize=8, fontweight=weight, color=COLORS['text'])

    # Rank annotation for target ticker
    sorted_returns = sorted(enumerate(returns), key=lambda x: -x[1])
    for rank, (idx, _) in enumerate(sorted_returns, 1):
        if peer_data[idx]['name'] == ticker:
            rank_text = L('rank').format(rank=rank, total=len(peer_data))
            ax.annotate(rank_text, xy=(0.98, 0.95),
                        xycoords='axes fraction', fontsize=9, fontweight='bold',
                        color=COLORS['primary'], ha='right', va='top',
                        bbox=dict(boxstyle='round,pad=0.3', facecolor=COLORS['very_light'],
                                 edgecolor=COLORS['light_gray']))
            break

    ax.axvline(x=0, color=COLORS['light_gray'], linewidth=0.8)
    ax.set_xlabel(L('5d_return'), fontsize=8, color=COLORS['muted'])
    ax.set_title(f'{ticker} vs Peers — {L("peer_perf")}', fontsize=12,
                 fontweight='bold', color=COLORS['primary'], loc='left', pad=12)

    plt.tight_layout()
    fig.savefig(output_path, bbox_inches='tight', dpi=150)  # format inferred from extension (.svg)
    plt.close(fig)
    print(f'  Generated: {output_path}')


def main():
    global LANG

    if len(sys.argv) < 4:
        print('Usage: .venv/bin/python3 templates/charts.py <workspace> <date> <ticker> [--lang ch] [catalysts_json]')
        sys.exit(1)

    workspace = sys.argv[1]
    date = sys.argv[2]
    ticker = sys.argv[3]

    # Parse --lang flag
    remaining_args = sys.argv[4:]
    if '--lang' in remaining_args:
        idx = remaining_args.index('--lang')
        if idx + 1 < len(remaining_args):
            LANG = remaining_args[idx + 1]
            remaining_args = remaining_args[:idx] + remaining_args[idx+2:]
    else:
        # Try reading from resolved_config.json
        rc_path = Path(workspace) / 'resolved_config.json'
        if rc_path.exists():
            try:
                with open(rc_path) as f:
                    rc = json.load(f)
                LANG = rc.get('language', 'en')
            except Exception:
                pass

    _setup_rcparams()

    # Load catalysts if provided
    catalysts = None
    def _load_catalysts(data):
        """Extract catalyst list from catalysts.json, supporting multiple key layouts."""
        return (data.get('catalysts')
                or data.get('upcoming_catalysts')
                or data.get('events')
                or [])

    if remaining_args:
        try:
            with open(remaining_args[0]) as f:
                cat_data = json.load(f)
                catalysts = _load_catalysts(cat_data)
        except Exception:
            pass

    # Also try workspace catalysts file
    if catalysts is None:
        for cat_path in [Path(workspace) / 'raw' / date / 'calendar' / 'catalysts.json',
                         Path(workspace) / 'raw' / 'calendar' / 'catalysts.json']:
            if cat_path.exists():
                try:
                    with open(cat_path) as f:
                        catalysts = _load_catalysts(json.load(f))
                    break
                except Exception:
                    pass

    chart_dir = Path(workspace) / 'exports' / date / 'pdf' / 'charts'
    chart_dir.mkdir(parents=True, exist_ok=True)

    raw_prices = Path(workspace) / 'raw' / date / 'prices'
    if not raw_prices.exists():
        raw_prices = Path(workspace) / 'raw' / 'prices'

    print(f'Generating annotated charts for {ticker} ({date})...')

    # Ticker variants: "2268.HK" -> also try "2268HK", "2268_HK"
    ticker_clean = ticker.replace('.', '')
    ticker_under = ticker.replace('.', '_')

    # 1. Price chart (annotated)
    ticker_3mo = raw_prices / f'{ticker}_3mo.csv'
    if not ticker_3mo.exists():
        ticker_3mo = raw_prices / f'{ticker_clean}_3mo.csv'
    if not ticker_3mo.exists():
        ticker_3mo = raw_prices / f'{ticker_under}_3mo.csv'
    if not ticker_3mo.exists():
        ticker_3mo = raw_prices / f'{ticker}_medium.csv'
    if not ticker_3mo.exists():
        ticker_3mo = raw_prices / f'{ticker_clean}_medium.csv'
    if ticker_3mo.exists():
        generate_price_chart(ticker, str(ticker_3mo), str(chart_dir / 'price_chart.svg'),
                            catalysts=catalysts, run_date=date)

    # 2. Relative performance chart (annotated)
    # Try multiple index names: SPY, HSI, ^HSI, sector_etf, etc.
    spy_path = None
    index_candidates = ['SPY', 'HSI', '^HSI', 'sector_etf', 'QQQ', '^GSPC']
    search_paths = []
    for idx_name in index_candidates:
        search_paths.append(raw_prices / f'{idx_name}_3mo.csv')
        search_paths.append(raw_prices / f'{idx_name}_medium.csv')
    # Shared market context paths (various naming conventions)
    shared_mc = Path(workspace) / '..' / 'shared' / 'market_context'
    search_paths.extend([
        shared_mc / date / 'raw' / 'SPY_3mo.csv',
        shared_mc / date / 'raw' / 'SPY_prices.csv',
        shared_mc / 'raw' / 'SPY_3mo.csv',
        shared_mc / 'raw' / 'SPY_prices.csv',
        shared_mc / date / 'raw' / 'QQQ_prices.csv',
    ])
    for candidate in search_paths:
        if candidate.exists():
            spy_path = candidate
            break

    if ticker_3mo.exists() and spy_path:
        # Derive index display name from filename
        idx_name = spy_path.stem.replace('_3mo', '').replace('_medium', '').replace('_', ' ').upper()
        if idx_name == 'SECTOR ETF':
            idx_name = 'Sector'
        generate_relative_chart(ticker, str(ticker_3mo), str(spy_path),
                                str(chart_dir / 'relative_chart.svg'),
                                index_name=idx_name)

    # 3. Peer chart (target highlighted)
    peer_path = Path(workspace) / 'profile' / 'peer_set.json'
    if peer_path.exists() and raw_prices.exists():
        try:
            with open(peer_path) as f:
                peer_set = json.load(f)
            peers = peer_set.get('peers', [])
            peer_data = []
            for p in peers[:5]:
                pticker = p['ticker']
                pfile = raw_prices / f'{pticker}_3mo.csv'
                if not pfile.exists():
                    pfile = raw_prices / f'{pticker}_medium.csv'
                if not pfile.exists():
                    pfile = raw_prices / f'peer_{pticker}.csv'
                if pfile.exists():
                    pdf = load_price_csv(str(pfile))
                    if len(pdf) >= 5:
                        ret = (pdf['Close'].iloc[-1] / pdf['Close'].iloc[-5] - 1) * 100
                        peer_data.append({'name': pticker, 'return_5d': float(ret)})
            # Add target ticker
            if ticker_3mo.exists():
                tdf = load_price_csv(str(ticker_3mo))
                if len(tdf) >= 5:
                    tret = (tdf['Close'].iloc[-1] / tdf['Close'].iloc[-5] - 1) * 100
                    peer_data.insert(0, {'name': ticker, 'return_5d': float(tret)})
            if peer_data:
                generate_peer_chart(ticker, peer_data, str(chart_dir / 'peer_chart.svg'))
        except Exception as e:
            print(f'  Warning: peer chart failed: {e}')

    print(f'Charts saved to: {chart_dir}')


if __name__ == '__main__':
    main()
