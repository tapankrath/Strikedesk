"""
StrikeDesk nightly data builder.

Pulls end-of-day options data from Yahoo Finance (via the unofficial `yfinance`
library — free, no API key, but not officially supported by Yahoo and can break
or rate-limit without warning) and computes the fields the StrikeDesk UI expects,
writing them to data.json at the repo root.

IMPORTANT — read before trusting the numbers:
- `iv` (implied volatility) comes directly from Yahoo's option chain.
- `delta` is computed here via Black-Scholes, assuming 0% dividend yield and a
  flat risk-free rate (RISK_FREE_RATE below). Real delta from a broker may differ.
- `pot` (probability of touch) uses the common trader heuristic pot ≈ 2 × |delta|,
  not a rigorous barrier-option calculation. Treat it as a rough guide.
- `ivr` (IV Rank) is NOT true IV rank (which needs a year of historical *option*
  IV data, which isn't freely available). It's a proxy built from the percentile
  of recent 20-day realized volatility vs. the past year — correlated with real
  IV rank but not the same number your broker would show.
- `score` (composite rating, 0-10) is an illustrative weighted blend of the above.
  It is not a validated trading signal. Adjust the weights in `composite_score()`
  to match what you actually care about.
- Strategy/strike selection targets a ~0.20 delta short leg, a common informal
  "20-delta" premium-selling convention — not personalized to any risk tolerance.

This script is a starting point, not a finished quant model. Treat every number
it produces as directional, not authoritative, and verify anything before
acting on it.
"""

import json
import math
import sys
from datetime import datetime, timezone

import numpy as np
from scipy.stats import norm

try:
    import yfinance as yf
except ImportError:
    print("yfinance not installed — run: pip install -r scripts/requirements.txt", file=sys.stderr)
    raise

# --- Configuration -----------------------------------------------------------

def load_tickers():
    """
    Reads the watchlist from tickers.json (repo root) so it can be edited without
    touching this script — either by hand on GitHub, or via the "Manage Tickers"
    panel in the app, which generates ready-to-paste JSON for this file.
    Falls back to a small built-in default set if the file is missing or invalid,
    so a bad edit here can't break the nightly run entirely.
    """
    default_tickers = ["AAPL", "MSFT", "NVDA", "XOM", "JPM", "SPY", "META", "TSLA", "AMD"]
    default_etfs = ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "GLD"]
    try:
        with open("tickers.json") as f:
            cfg = json.load(f)
        tickers = cfg.get("tickers") or default_tickers
        etfs = set(cfg.get("etfs") or default_etfs)
        return [t.strip().upper() for t in tickers if t.strip()], etfs
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"tickers.json missing or invalid ({e}) — using built-in defaults", file=sys.stderr)
        return default_tickers, set(default_etfs)


TICKERS, KNOWN_ETFS = load_tickers()

TARGET_DTE_MIN = 14
TARGET_DTE_MAX = 55
TARGET_SHORT_DELTA = 0.20   # informal "20-delta" premium-selling target
RISK_FREE_RATE = 0.045      # flat approximation; update periodically
OUTPUT_PATH = "data.json"


# --- Math helpers --------------------------------------------------------------

def bs_delta(spot, strike, dte_days, iv, option_type, r=RISK_FREE_RATE):
    """Black-Scholes delta. option_type: 'call' or 'put'. Assumes 0% dividend yield."""
    if dte_days <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    t = dte_days / 365.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv ** 2) * t) / (iv * math.sqrt(t))
    if option_type == "call":
        return float(norm.cdf(d1))
    return float(norm.cdf(d1) - 1)


def probability_of_touch(delta):
    """Rough trader heuristic, not a rigorous barrier-option calculation."""
    return min(100, round(abs(delta) * 2 * 100))


def compute_ema(closes, span):
    return closes.ewm(span=span, adjust=False).mean()


def compute_atr(history, period=14):
    high, low, close = history["High"], history["Low"], history["Close"]
    prev_close = close.shift(1)
    tr = np.maximum(high - low, np.maximum((high - prev_close).abs(), (low - prev_close).abs()))
    return tr.rolling(period).mean().iloc[-1]


def iv_rank_proxy(history, window=252, vol_window=20):
    """
    Proxy for IV rank using realized volatility percentile, since a year of
    historical *implied* volatility isn't freely available. Correlated with
    real IV rank but not equivalent to it.
    """
    closes = history["Close"].tail(window + vol_window)
    log_returns = np.log(closes / closes.shift(1)).dropna()
    realized_vol = log_returns.rolling(vol_window).std() * math.sqrt(252)
    realized_vol = realized_vol.dropna()
    if len(realized_vol) < 20:
        return 50  # not enough history yet — neutral fallback
    current = realized_vol.iloc[-1]
    percentile = (realized_vol < current).sum() / len(realized_vol) * 100
    return round(percentile)


def composite_score(ann_profit, pot, ivr):
    """Illustrative 0-10 blend — adjust weights to match your priorities."""
    profit_component = min(10, max(0, ann_profit / 5))      # ~50% ann. profit -> 10
    safety_component = min(10, max(0, (100 - pot) / 10))     # lower POT -> higher score
    ivr_component = min(10, max(0, ivr / 10))
    score = 0.45 * profit_component + 0.35 * safety_component + 0.20 * ivr_component
    return round(min(10, max(1, score)), 1)


def pick_strike_by_delta(chain_df, spot, dte_days, target_delta, option_type):
    """Return the chain row whose computed delta is closest to target_delta."""
    best_row, best_diff = None, None
    for _, row in chain_df.iterrows():
        iv = row.get("impliedVolatility") or 0
        if iv <= 0:
            continue
        delta = bs_delta(spot, row["strike"], dte_days, iv, option_type)
        diff = abs(abs(delta) - target_delta)
        if best_diff is None or diff < best_diff:
            best_diff, best_row = diff, (row, delta)
    return best_row  # (row, delta) or None


def mid_price(row):
    bid, ask = row.get("bid") or 0, row.get("ask") or 0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return row.get("lastPrice") or 0


# --- Per-ticker trade construction --------------------------------------------

def pick_expiration(expirations, today):
    best, best_diff = None, None
    for exp_str in expirations:
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        dte = (exp_date - today).days
        if TARGET_DTE_MIN <= dte <= TARGET_DTE_MAX:
            diff = abs(dte - (TARGET_DTE_MIN + TARGET_DTE_MAX) / 2)
            if best_diff is None or diff < best_diff:
                best, best_diff = (exp_str, dte), diff
    if best:
        return best
    # fallback: nearest expiration to the target window, even if outside it
    for exp_str in expirations:
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        dte = (exp_date - today).days
        if dte > 0:
            return (exp_str, dte)
    return None


def build_trade_for_ticker(ticker_symbol, index):
    try:
        tk = yf.Ticker(ticker_symbol)
        history = tk.history(period="1y")
        if history.empty:
            print(f"  skip {ticker_symbol}: no price history")
            return None

        spot = float(history["Close"].iloc[-1])
        ema8 = compute_ema(history["Close"], 8).iloc[-1]
        ema20 = compute_ema(history["Close"], 20).iloc[-1]
        uptrend = ema8 > ema20
        near_ema = (abs(spot - ema8) / spot < 0.015) or (abs(spot - ema20) / spot < 0.015)
        atr = compute_atr(history)
        ivr = iv_rank_proxy(history)

        today = datetime.now(timezone.utc).date()
        expirations = tk.options
        if not expirations:
            print(f"  skip {ticker_symbol}: no options listed")
            return None
        picked = pick_expiration(expirations, today)
        if not picked:
            print(f"  skip {ticker_symbol}: no usable expiration")
            return None
        exp_str, dte = picked

        chain = tk.option_chain(exp_str)
        calls, puts = chain.calls, chain.puts

        total_call_oi = calls["openInterest"].fillna(0).sum()
        total_put_oi = puts["openInterest"].fillna(0).sum()
        total_call_vol = calls["volume"].fillna(0).sum()
        total_put_vol = puts["volume"].fillna(0).sum()
        pc_oi = round(total_put_oi / total_call_oi, 2) if total_call_oi else 0
        pc_vol = round(total_put_vol / total_call_vol, 2) if total_call_vol else 0

        # earnings within the option's window?
        earnings_soon = False
        try:
            edates = tk.get_earnings_dates(limit=4)
            if edates is not None and not edates.empty:
                for dt in edates.index:
                    d = dt.date() if hasattr(dt, "date") else dt
                    if 0 <= (d - today).days <= dte:
                        earnings_soon = True
                        break
        except Exception:
            pass  # earnings calendar not always available — leave as False

        is_etf = ticker_symbol in KNOWN_ETFS

        # strategy selection: uptrend -> bullish rotation, downtrend -> bearish rotation
        if uptrend:
            strat = ["Short Put", "Covered Call", "Bull Put Spread"][index % 3]
            side = "bull"
        else:
            strat = ["Short Call", "Bear Call Spread"][index % 2]
            side = "bear"

        if strat == "Short Put":
            picked_row = pick_strike_by_delta(puts, spot, dte, TARGET_SHORT_DELTA, "put")
            if not picked_row:
                return None
            row, delta = picked_row
            premium = mid_price(row)
            strike = float(row["strike"])
            collateral = strike
            breakeven = strike - premium
            max_loss = round((strike - premium) * 100, 2)
            strike_label = f"${strike:.0f} P"
            iv = float(row.get("impliedVolatility") or 0) * 100

        elif strat == "Covered Call":
            picked_row = pick_strike_by_delta(calls, spot, dte, TARGET_SHORT_DELTA, "call")
            if not picked_row:
                return None
            row, delta = picked_row
            premium = mid_price(row)
            strike = float(row["strike"])
            collateral = spot
            breakeven = spot - premium
            max_loss = round((spot - premium) * 100, 2)
            strike_label = f"${strike:.0f} C"
            iv = float(row.get("impliedVolatility") or 0) * 100

        elif strat == "Short Call":
            picked_row = pick_strike_by_delta(calls, spot, dte, TARGET_SHORT_DELTA, "call")
            if not picked_row:
                return None
            row, delta = picked_row
            premium = mid_price(row)
            strike = float(row["strike"])
            collateral = strike  # rough proxy; true naked-call risk is undefined
            breakeven = strike + premium
            max_loss = round(strike * 100, 2)  # illustrative cap, not a real max-loss figure
            strike_label = f"${strike:.0f} C"
            iv = float(row.get("impliedVolatility") or 0) * 100

        elif strat == "Bull Put Spread":
            short_row = pick_strike_by_delta(puts, spot, dte, TARGET_SHORT_DELTA, "put")
            if not short_row:
                return None
            s_row, s_delta = short_row
            short_strike = float(s_row["strike"])
            lower_strikes = puts[puts["strike"] < short_strike].sort_values("strike", ascending=False)
            if lower_strikes.empty:
                return None
            long_row = lower_strikes.iloc[min(1, len(lower_strikes) - 1)]  # a couple strikes further OTM
            long_strike = float(long_row["strike"])
            premium = mid_price(s_row) - mid_price(long_row)
            width = short_strike - long_strike
            collateral = width
            breakeven = short_strike - premium
            max_loss = round((width - premium) * 100, 2)
            strike_label = f"${short_strike:.0f}/{long_strike:.0f}"
            iv = float(s_row.get("impliedVolatility") or 0) * 100

        else:  # Bear Call Spread
            short_row = pick_strike_by_delta(calls, spot, dte, TARGET_SHORT_DELTA, "call")
            if not short_row:
                return None
            s_row, s_delta = short_row
            short_strike = float(s_row["strike"])
            higher_strikes = calls[calls["strike"] > short_strike].sort_values("strike")
            if higher_strikes.empty:
                return None
            long_row = higher_strikes.iloc[min(1, len(higher_strikes) - 1)]
            long_strike = float(long_row["strike"])
            premium = mid_price(s_row) - mid_price(long_row)
            width = long_strike - short_strike
            collateral = width
            breakeven = short_strike + premium
            max_loss = round((width - premium) * 100, 2)
            strike_label = f"${short_strike:.0f}/{long_strike:.0f}"
            iv = float(s_row.get("impliedVolatility") or 0) * 100

        if premium <= 0 or collateral <= 0:
            print(f"  skip {ticker_symbol}: unusable premium/collateral")
            return None

        roc = round((premium / collateral) * 100, 2)
        ann_profit = round(roc * (365 / dte), 1)

        # sanity guard: thin/degenerate spreads (tiny premium on a narrow width) can
        # produce nonsensical annualized figures — reject rather than publish junk
        if ann_profit > 150 or ann_profit < -50:
            print(f"  skip {ticker_symbol}: implausible annualized profit ({ann_profit}%), likely a thin/degenerate quote")
            return None

        daily_return = round(premium * 100 / dte, 2)
        pot = probability_of_touch(bs_delta(spot, strike if strat != "Bull Put Spread" and strat != "Bear Call Spread" else short_strike, dte, iv / 100 if iv else 0.3, "put" if side == "bull" else "call"))
        margin_of_safety = bool(atr and abs(spot - (strike if strat not in ("Bull Put Spread", "Bear Call Spread") else short_strike)) >= atr)
        exp_label = datetime.strptime(exp_str, "%Y-%m-%d").strftime("%b %-d") if sys.platform != "win32" else datetime.strptime(exp_str, "%Y-%m-%d").strftime("%b %d").replace(" 0", " ")
        score = composite_score(ann_profit, pot, ivr)

        return {
            "sym": ticker_symbol,
            "strat": strat,
            "side": side,
            "isETF": is_etf,
            "strike": strike_label,
            "exp": exp_label,
            "dte": dte,
            "pot": pot,
            "ap": ann_profit,
            "ivr": ivr,
            "dailyReturn": daily_return,
            "roc": roc,
            "score": score,
            "buy": bool(uptrend),
            "sell": bool(not uptrend),
            "ema": bool(near_ema),
            "earningsSoon": earnings_soon,
            "marginOfSafety": margin_of_safety,
            "delta": round(s_delta if strat in ("Bull Put Spread", "Bear Call Spread") else delta, 2),
            "iv": round(iv, 1),
            "premium": round(premium, 2),
            "breakeven": round(breakeven, 2),
            "maxLoss": max_loss,
            "pcOI": pc_oi,
            "pcVol": pc_vol,
        }

    except Exception as e:
        print(f"  skip {ticker_symbol}: {e}")
        return None


def main():
    trades = []
    for i, ticker in enumerate(TICKERS):
        print(f"Fetching {ticker}...")
        trade = build_trade_for_ticker(ticker, i)
        if trade:
            trades.append(trade)

    if not trades:
        print("No trades were built — leaving existing data.json untouched.", file=sys.stderr)
        sys.exit(1)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "yfinance (unofficial, free, EOD)",
        "trades": trades,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote {len(trades)} trades to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
