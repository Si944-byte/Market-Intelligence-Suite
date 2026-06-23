"""
backtest_signals (public use).py
=================================
BacktestRegime — Step 3: Signal Generator (v2)
Database: BacktestRegime
Table:    dbo.signals_mart

Corrected Signal Sequence:
    1. 4H  — Confirms DIRECTION
               Last closed 4H bar: close > open AND close > prior close = bullish
               Last closed 4H bar: close < open AND close < prior close = bearish

    2. 1H  — Confirms BREAK OF STRUCTURE
               A 1H candle closes beyond a key level in the 4H direction.
               Primary level  : Previous Day High (bullish) / Previous Day Low (bearish)
               Secondary level: Swing High / Swing Low (10-bar lookback)
               Wick breaks do NOT count — close only.

    3. 5M  — Confirms DISPLACEMENT at the BoS level
               Large 5M candle closes at/near the 1H BoS level.
               Body > 20 ticks AND >= 1.5x 20-bar average body.
               BEARISH displacement = REVERSAL  (short setup)
               BULLISH displacement = CONTINUATION (long setup)
               News filter applied here (+-5 min of red folder event).

    4. 5M  — PULLBACK ENTRY
               Subsequent 5M candle closes strictly inside the zone.
               Zone = displacement candle body (top/bottom).
               Zone violation before entry = signal cancelled.

Signal Quality:
    A = PDH/PDL level + prior sweep detected + confluence (PDH/PDL near swing)
    B = PDH/PDL level + displacement (no prior sweep)
    C = Swing level only + displacement

Signal Type:
    REVERSAL     = bearish displacement at level (fade)
    CONTINUATION = bullish displacement at level (with BoS direction)

Usage:
    python "backtest_signals (public use).py"
    python "backtest_signals (public use).py" --instrument MES1
    python "backtest_signals (public use).py" --clear

Dependencies:
    pip install pandas pyodbc numpy
"""

import sys
import argparse
import numpy as np
import pandas as pd
import pyodbc
from datetime import timedelta

# Force UTF-8 output on Windows to prevent cp1252 UnicodeEncodeError
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ============================================================================
# CONFIGURATION — update these values for your environment
# ============================================================================
# Recommended: store credentials in a .env file and load with python-dotenv
# See README.md for setup instructions

SQL_SERVER        = "YOUR_SQL_SERVER"
SQL_DATABASE      = "BacktestRegime"
CONNECTION_STRING = (
    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
    f"SERVER={SQL_SERVER};"
    f"DATABASE={SQL_DATABASE};"
    f"UID=YOUR_SQL_USER;PWD=YOUR_SQL_PASSWORD;"
)

# ── Strategy Parameters ──────────────────────────────────────────────────────
SWING_LOOKBACK         = 10     # Bars for swing high/low detection on 1H
AVG_BODY_LOOKBACK      = 20     # Bars for average body calculation on 5M
DISPLACEMENT_RATIO     = 1.5    # Min body size vs 20-bar avg
MIN_DISPLACEMENT_TICKS = 20     # Min displacement in ticks
ZONE_EXPIRY_BARS       = 50     # Zone expires after X 5M bars
NEWS_FILTER_MINUTES    = 5      # Skip signals within X min of news
STOP_BUFFER_TICKS      = 2      # Extra ticks beyond displacement swing for stop
PDL_PROXIMITY_TICKS    = 10     # How close 5M displacement must be to BoS level
SWEEP_LOOKBACK_BARS    = 20     # How many 1H bars back to look for prior sweep

# ── Tick sizes ───────────────────────────────────────────────────────────────
TICK_SIZES = {
    "MES1": 0.25,  "MNQ1": 0.25,  "MYM1": 1.0,
    "MGC1": 0.10,  "MCL1": 0.01,  "SIL1": 0.005,
    "CL1":  0.01,  "NG1":  0.001, "6E1":  0.00005,
    "ZN1":  0.015625, "ZB1": 0.03125,
    "ZC1":  0.25,  "ZS1":  0.25,
}

# ── Contract sizes ───────────────────────────────────────────────────────────
CONTRACT_SIZES = {
    "MES1": 3, "MNQ1": 3, "MYM1": 3,
    "MGC1": 2, "MCL1": 2, "6E1":  2,
    "ZC1":  2, "ZS1":  2,
    "SIL1": 1, "ZN1":  1, "ZB1":  1,
    "CL1":  3, "NG1":  3,
}


# ============================================================================
# DATABASE HELPERS
# ============================================================================

def get_connection():
    try:
        return pyodbc.connect(CONNECTION_STRING, autocommit=False)
    except pyodbc.Error as e:
        print(f"\n[ERROR] Connection failed: {e}")
        sys.exit(1)


def load_prices(conn, instrument, timeframe):
    sql = """
        SELECT bar_time, open_price, high_price, low_price, close_price, volume
        FROM dbo.raw_prices
        WHERE instrument = ? AND timeframe = ?
        ORDER BY bar_time ASC
    """
    df = pd.read_sql(sql, conn, params=(instrument, timeframe))
    df["bar_time"] = pd.to_datetime(df["bar_time"])
    df.columns = ["bar_time", "open", "high", "low", "close", "volume"]
    return df.reset_index(drop=True)


def load_news_events(conn):
    sql = "SELECT event_time FROM dbo.news_events ORDER BY event_time ASC"
    df  = pd.read_sql(sql, conn)
    df["event_time"] = pd.to_datetime(df["event_time"])
    return df["event_time"].sort_values().reset_index(drop=True)


def clear_signals(conn, instrument=None):
    cursor = conn.cursor()
    if instrument:
        cursor.execute("""
            DELETE FROM dbo.trade_mart
            WHERE signal_id IN (
                SELECT signal_id FROM dbo.signals_mart
                WHERE instrument = ?
            )
        """, instrument)
        cursor.execute("""
            DELETE FROM dbo.equity_curve_daily
            WHERE instrument = ?
        """, instrument)
        cursor.execute("""
            DELETE FROM dbo.equity_curve_daily
            WHERE instrument = 'ALL'
        """)
        cursor.execute("""
            DELETE FROM dbo.regime_tags
            WHERE signal_id IN (
                SELECT signal_id FROM dbo.signals_mart
                WHERE instrument = ?
            )
        """, instrument)
        cursor.execute(
            "DELETE FROM dbo.signals_mart WHERE instrument = ?", instrument
        )
    else:
        # Delete all child tables first then parent
        cursor.execute("DELETE FROM dbo.trade_mart")
        cursor.execute("DELETE FROM dbo.equity_curve_daily")
        cursor.execute("DELETE FROM dbo.regime_tags")
        cursor.execute("DELETE FROM dbo.signals_mart")
    conn.commit()
    print(f"  Cleared signals{' for ' + instrument if instrument else ' (all)'}.")


def insert_signal(cursor, sig):
    sql = """
        INSERT INTO dbo.signals_mart (
            instrument, direction, signal_type, signal_quality,
            sweep_time, sweep_level, sweep_type,
            prior_sweep, bos_level_type,
            displacement_time, displacement_open, displacement_high,
            displacement_low, displacement_close, displacement_body_size,
            displacement_ticks, avg_body_size, displacement_ratio,
            zone_top, zone_bottom,
            bos_time, bos_level,
            entry_time, entry_price,
            stop_price, stop_distance_ticks, target1_price,
            contracts,
            passed_tick_filter, passed_news_filter, signal_valid,
            h1_bos_bullish, h4_bias_bullish, timeframes_aligned
        ) VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
    """
    cursor.execute(sql, (
        sig["instrument"],      sig["direction"],
        sig["signal_type"],     sig["signal_quality"],
        sig["sweep_time"],      sig["sweep_level"],     sig["sweep_type"],
        sig["prior_sweep"],     sig["bos_level_type"],
        sig["displ_time"],      sig["displ_open"],      sig["displ_high"],
        sig["displ_low"],       sig["displ_close"],     sig["displ_body"],
        sig["displ_ticks"],     sig["avg_body"],        sig["displ_ratio"],
        sig["zone_top"],        sig["zone_bottom"],
        sig["bos_time"],        sig["bos_level"],
        sig["entry_time"],      sig["entry_price"],
        sig["stop_price"],      sig["stop_ticks"],      sig["target1"],
        sig["contracts"],
        sig["pass_tick"],       sig["pass_news"],       sig["valid"],
        sig["h1_bos_bullish"],  sig["h4_bias_bullish"], sig["aligned"]
    ))


# ============================================================================
# CALCULATION HELPERS
# ============================================================================

def calc_swing_highs(highs, lookback):
    n      = len(highs)
    result = np.full(n, np.nan)
    for i in range(lookback, n):
        result[i] = np.max(highs[i - lookback : i])
    return result


def calc_swing_lows(lows, lookback):
    n      = len(lows)
    result = np.full(n, np.nan)
    for i in range(lookback, n):
        result[i] = np.min(lows[i - lookback : i])
    return result


def calc_avg_body(opens, closes, lookback):
    bodies = np.abs(closes - opens)
    n      = len(bodies)
    result = np.full(n, np.nan)
    for i in range(lookback, n):
        result[i] = np.mean(bodies[i - lookback : i])
    return result


def calc_prev_day_hl(df_1h):
    """
    For each 1H bar calculate the previous calendar day's high and low.
    Uses the 1H bars themselves — groups by date, shifts by one day.
    Returns DataFrame with columns: bar_time, prev_day_high, prev_day_low
    """
    df = df_1h.copy()
    df["date"] = df["bar_time"].dt.date

    daily = df.groupby("date").agg(
        day_high=("high", "max"),
        day_low=("low",  "min")
    ).reset_index()

    daily["prev_day_high"] = daily["day_high"].shift(1)
    daily["prev_day_low"]  = daily["day_low"].shift(1)

    df = df.merge(
        daily[["date", "prev_day_high", "prev_day_low"]],
        on="date", how="left"
    )
    return df[["bar_time", "prev_day_high", "prev_day_low"]]


def is_near_news(bar_time, news_times, window_minutes=5):
    if len(news_times) == 0:
        return False
    window = timedelta(minutes=window_minutes)
    mask   = (
        (news_times >= bar_time - window) &
        (news_times <= bar_time + window)
    )
    return bool(mask.any())


# ============================================================================
# STEP 1: 4H DIRECTION
# ============================================================================

def get_4h_direction(df_4h, bar_time):
    """
    Returns 'bullish', 'bearish', or 'neutral'.
    Bullish: last closed 4H close > open AND close > prior close
    Bearish: last closed 4H close < open AND close < prior close
    """
    past = df_4h[df_4h["bar_time"] < bar_time]
    if len(past) < 2:
        return "neutral"
    last  = past.iloc[-1]
    prior = past.iloc[-2]
    if last["close"] > last["open"] and last["close"] > prior["close"]:
        return "bullish"
    elif last["close"] < last["open"] and last["close"] < prior["close"]:
        return "bearish"
    return "neutral"


# ============================================================================
# STEP 2: 1H BoS DETECTION
# Returns dict with bos details or None
# ============================================================================

def find_1h_bos(df_1h, pdhl_df, h4_direction, from_time, swing_lookback):
    """
    Scans 1H bars from from_time onward looking for a BoS in h4_direction.

    For BULLISH h4:
        BoS = 1H bar closes ABOVE Previous Day High (primary)
           OR 1H bar closes ABOVE Swing High (secondary)

    For BEARISH h4:
        BoS = 1H bar closes BELOW Previous Day Low (primary)
           OR 1H bar closes BELOW Swing Low (secondary)

    Returns first valid BoS found as dict, or None.
    """
    # Get 1H bars from from_time onward
    df = df_1h[df_1h["bar_time"] >= from_time].copy().reset_index(drop=True)
    if len(df) < swing_lookback + 2:
        return None

    # Merge PDH/PDL
    df = df.merge(pdhl_df, on="bar_time", how="left")

    # Pre-calculate swing levels
    highs       = df["high"].values
    lows        = df["low"].values
    closes      = df["close"].values
    swing_highs = calc_swing_highs(highs, swing_lookback)
    swing_lows  = calc_swing_lows(lows,   swing_lookback)

    for i in range(swing_lookback + 1, len(df)):
        bar      = df.iloc[i]
        pdh      = bar["prev_day_high"]
        pdl      = bar["prev_day_low"]
        sh       = swing_highs[i - 1]
        sl       = swing_lows[i - 1]

        if h4_direction == "bullish":
            # Primary: close above PDH
            if not pd.isna(pdh) and closes[i] > pdh:
                return {
                    "bos_time":       bar["bar_time"],
                    "bos_level":      float(pdh),
                    "bos_level_type": "PDH",
                    "bos_bullish":    True,
                }
            # Swing fallback disabled — PDH/PDL only

        elif h4_direction == "bearish":
            # Primary: close below PDL
            if not pd.isna(pdl) and closes[i] < pdl:
                return {
                    "bos_time":       bar["bar_time"],
                    "bos_level":      float(pdl),
                    "bos_level_type": "PDL",
                    "bos_bullish":    False,
                }
            # Swing fallback disabled — PDH/PDL only

    return None


# ============================================================================
# STEP 2b: PRIOR SWEEP DETECTION (for quality grading)
# Looks back on 1H for a sweep of the opposite side before the BoS
# ============================================================================

def check_prior_sweep(df_1h, bos_time, bos_level, bos_bullish,
                      pdhl_df, lookback_bars=20):
    """
    Checks if there was a sweep of the opposite key level before the BoS.
    Bullish BoS: was there a sweep BELOW PDL/swing low before price broke up?
    Bearish BoS: was there a sweep ABOVE PDH/swing high before price broke down?
    Returns True if prior sweep detected.
    """
    df = df_1h[df_1h["bar_time"] < bos_time].tail(lookback_bars).copy()
    if len(df) == 0:
        return False

    df = df.merge(pdhl_df, on="bar_time", how="left")

    if bos_bullish:
        # Look for wick below PDL (sweep of sell-side liquidity)
        for _, bar in df.iterrows():
            pdl = bar.get("prev_day_low")
            if not pd.isna(pdl) and bar["low"] < pdl:
                return True
    else:
        # Look for wick above PDH (sweep of buy-side liquidity)
        for _, bar in df.iterrows():
            pdh = bar.get("prev_day_high")
            if not pd.isna(pdh) and bar["high"] > pdh:
                return True

    return False


# ============================================================================
# STEP 3: 5M DISPLACEMENT AT BoS LEVEL
# ============================================================================

def find_5m_displacement(df_5m, bos_time, bos_level, tick_size,
                         avg_body_lookup, news_times):
    """
    Scans 5M bars after bos_time looking for a displacement candle
    at/near the bos_level.

    Proximity check: displacement candle high or low must be within
    PDL_PROXIMITY_TICKS of the bos_level.

    Returns dict of displacement details or None.
    """
    proximity = PDL_PROXIMITY_TICKS * tick_size

    # Get 5M bars after BoS
    df = df_5m[df_5m["bar_time"] > bos_time].copy().reset_index(drop=True)
    if df.empty:
        return None

    opens  = df["open"].values
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    times  = df["bar_time"].values

    for i in range(len(df)):
        bar_time_i = pd.Timestamp(times[i])
        body       = abs(closes[i] - opens[i])

        avg_body = avg_body_lookup.get(bar_time_i)
        if avg_body is None or np.isnan(avg_body) or avg_body == 0:
            continue

        ratio = body / avg_body
        ticks = body / tick_size

        # Must be large enough
        if ratio < DISPLACEMENT_RATIO or ticks < MIN_DISPLACEMENT_TICKS:
            continue

        # Must be near the BoS level
        # Candle high or low within proximity of bos_level
        near_level = (
            abs(highs[i]  - bos_level) <= proximity or
            abs(lows[i]   - bos_level) <= proximity or
            abs(closes[i] - bos_level) <= proximity or
            (lows[i] <= bos_level <= highs[i])  # candle spans the level
        )

        if not near_level:
            continue

        # News filter
        near_news = is_near_news(bar_time_i, news_times, NEWS_FILTER_MINUTES)

        # Determine displacement direction
        is_bullish = closes[i] > opens[i]
        is_bearish = closes[i] < opens[i]

        if not is_bullish and not is_bearish:
            continue  # Doji — skip

        return {
            "displ_time":    bar_time_i,
            "displ_open":    float(opens[i]),
            "displ_high":    float(highs[i]),
            "displ_low":     float(lows[i]),
            "displ_close":   float(closes[i]),
            "displ_body":    float(body),
            "displ_ticks":   float(ticks),
            "avg_body":      float(avg_body),
            "displ_ratio":   float(ratio),
            "displ_bullish": is_bullish,
            "near_news":     near_news,
            "zone_top":      float(max(opens[i], closes[i])),
            "zone_bottom":   float(min(opens[i], closes[i])),
        }

    return None


# ============================================================================
# STEP 4: PULLBACK ENTRY
# ============================================================================

def find_pullback_entry(df_5m, displ_time, zone_top, zone_bottom,
                        displ_bullish):
    """
    Scans 5M bars after displacement looking for a close strictly inside zone.
    Zone violation cancels the signal.
    Expires after ZONE_EXPIRY_BARS bars.
    Returns entry bar dict or None.
    """
    df = df_5m[df_5m["bar_time"] > displ_time].head(ZONE_EXPIRY_BARS).copy()
    if df.empty:
        return None

    for _, bar in df.iterrows():
        # Zone violation check
        if displ_bullish:
            # Bullish zone (demand) — violated if price closes below zone bottom
            if bar["close"] < zone_bottom:
                return None
        else:
            # Bearish zone (supply) — violated if price closes above zone top
            if bar["close"] > zone_top:
                return None

        # Entry: close strictly inside zone
        if zone_bottom < bar["close"] < zone_top:
            return {
                "entry_time":  bar["bar_time"],
                "entry_price": float(bar["close"]),
            }

    return None


# ============================================================================
# SIGNAL QUALITY GRADING
# ============================================================================

def grade_signal(bos_level_type, prior_sweep, pdh_near_swing):
    """
    A = PDH/PDL level + prior sweep + confluence (PDH near swing)
    B = PDH/PDL level + displacement (no prior sweep or no confluence)
    C = Swing level only
    """
    if bos_level_type in ("PDH", "PDL"):
        if prior_sweep and pdh_near_swing:
            return "A"
        else:
            return "B"
    else:
        return "C"


# ============================================================================
# MAIN SIGNAL DETECTION — one instrument
# ============================================================================

def detect_signals(instrument, df_5m, df_1h, df_4h, pdhl_df,
                   news_times, conn):
    tick_size = TICK_SIZES.get(instrument, 0.25)
    contracts = CONTRACT_SIZES.get(instrument, 1)

    # Pre-calculate 5M avg bodies — built once as a dict for O(1) lookup
    opens_5m        = df_5m["open"].values
    closes_5m       = df_5m["close"].values
    avg_bodies_arr  = calc_avg_body(opens_5m, closes_5m, AVG_BODY_LOOKBACK)
    avg_body_lookup = dict(zip(df_5m["bar_time"], avg_bodies_arr))

    cursor          = conn.cursor()
    signals_written = 0
    processed_bos   = set()

    # Determine scan start — need enough 4H bars for direction
    scan_start = df_4h["bar_time"].iloc[5] if len(df_4h) > 5 else df_4h["bar_time"].iloc[0]

    # Get list of 4H bar times to iterate
    h4_times = df_4h[df_4h["bar_time"] >= scan_start]["bar_time"].tolist()

    print(f"  Scanning {len(h4_times)} 4H bars...")

    last_bos_time = None

    for h4_bar_time in h4_times:

        # ── STEP 1: 4H Direction ─────────────────────────────────────────
        h4_direction = get_4h_direction(df_4h, h4_bar_time)
        if h4_direction == "neutral":
            continue

        # ── STEP 2: Find 1H BoS from this 4H bar forward ─────────────────
        bos = find_1h_bos(
            df_1h, pdhl_df, h4_direction,
            from_time=h4_bar_time,
            swing_lookback=SWING_LOOKBACK
        )
        if bos is None:
            continue

        # Skip if we already processed this BoS
        bos_key = (instrument, str(bos["bos_time"]))
        if bos_key in processed_bos:
            continue

        # Skip if a BoS was found within 10 1H bars of this one
        if last_bos_time is not None:
            h1_bars_since = len(df_1h[
                (df_1h["bar_time"] > last_bos_time) &
                (df_1h["bar_time"] <= bos["bos_time"])
            ])
            if h1_bars_since < 10:
                continue

        processed_bos.add(bos_key)
        last_bos_time = bos["bos_time"]

        # ── STEP 2b: Prior sweep check (quality grading) ──────────────────
        prior_sweep = check_prior_sweep(
            df_1h, bos["bos_time"], bos["bos_level"],
            bos["bos_bullish"], pdhl_df, SWEEP_LOOKBACK_BARS
        )

        # Check if PDH/PDL is near a swing level (confluence for A grade)
        # Simple check: any 1H swing within 5 ticks of the BoS level
        h1_near_bos = df_1h[df_1h["bar_time"] <= bos["bos_time"]].tail(20)
        swing_hs    = calc_swing_highs(h1_near_bos["high"].values, min(10, len(h1_near_bos)-1))
        swing_ls    = calc_swing_lows(h1_near_bos["low"].values,  min(10, len(h1_near_bos)-1))
        confluence_ticks = 5 * tick_size
        pdh_near_swing   = False
        if len(swing_hs) > 0 and len(swing_ls) > 0:
            last_sh = swing_hs[-1] if not np.isnan(swing_hs[-1]) else None
            last_sl = swing_ls[-1] if not np.isnan(swing_ls[-1]) else None
            if last_sh and abs(last_sh - bos["bos_level"]) <= confluence_ticks:
                pdh_near_swing = True
            if last_sl and abs(last_sl - bos["bos_level"]) <= confluence_ticks:
                pdh_near_swing = True

        # ── STEP 3: Find 5M displacement at BoS level ────────────────────
        displ = find_5m_displacement(
            df_5m, bos["bos_time"], bos["bos_level"],
            tick_size, avg_body_lookup, news_times
        )
        if displ is None:
            continue

        # ── STEP 4: Find pullback entry ───────────────────────────────────
        entry = find_pullback_entry(
            df_5m,
            displ["displ_time"],
            displ["zone_top"],
            displ["zone_bottom"],
            displ["displ_bullish"]
        )
        if entry is None:
            continue

        # ── BUILD SIGNAL ─────────────────────────────────────────────────

        # Signal type from displacement direction
        signal_type = "CONTINUATION" if displ["displ_bullish"] else "REVERSAL"

        # Direction from displacement
        direction = "LONG" if displ["displ_bullish"] else "SHORT"

        # Stop loss
        if direction == "LONG":
            stop_price = displ["displ_low"] - (STOP_BUFFER_TICKS * tick_size)
        else:
            stop_price = displ["displ_high"] + (STOP_BUFFER_TICKS * tick_size)

        stop_distance       = abs(entry["entry_price"] - stop_price)
        stop_distance_ticks = stop_distance / tick_size
        target1 = (
            entry["entry_price"] + stop_distance * 1.5 if direction == "LONG"
            else entry["entry_price"] - stop_distance * 1.5
        )

        # Quality grade
        quality = grade_signal(
            bos["bos_level_type"], prior_sweep, pdh_near_swing
        )

        # Filter flags
        pass_tick = displ["displ_ticks"] >= MIN_DISPLACEMENT_TICKS
        pass_news = not displ["near_news"]
        aligned   = h4_direction in (
            "bullish" if direction == "LONG" else "bearish",
        )
        valid = pass_tick and pass_news and aligned

        sig = {
            "instrument":    instrument,
            "direction":     direction,
            "signal_type":   signal_type,
            "signal_quality": quality,
            "sweep_time":    bos["bos_time"],     # BoS time = sweep reference
            "sweep_level":   bos["bos_level"],
            "sweep_type":    bos["bos_level_type"],
            "prior_sweep":   1 if prior_sweep else 0,
            "bos_level_type": bos["bos_level_type"],
            "displ_time":    displ["displ_time"],
            "displ_open":    displ["displ_open"],
            "displ_high":    displ["displ_high"],
            "displ_low":     displ["displ_low"],
            "displ_close":   displ["displ_close"],
            "displ_body":    displ["displ_body"],
            "displ_ticks":   displ["displ_ticks"],
            "avg_body":      displ["avg_body"],
            "displ_ratio":   displ["displ_ratio"],
            "zone_top":      displ["zone_top"],
            "zone_bottom":   displ["zone_bottom"],
            "bos_time":      bos["bos_time"],
            "bos_level":     bos["bos_level"],
            "entry_time":    entry["entry_time"],
            "entry_price":   entry["entry_price"],
            "stop_price":    float(stop_price),
            "stop_ticks":    float(stop_distance_ticks),
            "target1":       float(target1),
            "contracts":     contracts,
            "pass_tick":     1 if pass_tick else 0,
            "pass_news":     1 if pass_news else 0,
            "valid":         1 if valid else 0,
            "h1_bos_bullish": 1 if bos["bos_bullish"] else 0,
            "h4_bias_bullish": 1 if h4_direction == "bullish" else 0,
            "aligned":       1 if aligned else 0,
        }

        insert_signal(cursor, sig)
        conn.commit()
        signals_written += 1

        print(f"  Signal: {instrument} {direction} {signal_type} {quality} | "
              f"Entry {entry['entry_time']} @ {entry['entry_price']:.2f} | "
              f"BoS: {bos['bos_level_type']} {bos['bos_level']:.2f}")

    return signals_written


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="BacktestRegime Signal Generator v2")
    parser.add_argument("--instrument", type=str, default=None)
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("BacktestRegime — Signal Generator v2")
    print(f"Server : {SQL_SERVER}  |  DB: {SQL_DATABASE}")
    print("=" * 60)

    conn = get_connection()
    print("  Connected.")

    # Load news events
    print("\nLoading news events...")
    news_times = load_news_events(conn)
    print(f"  {len(news_times):,} events loaded.")

    # Get instruments
    cursor = conn.cursor()
    if args.instrument:
        instruments = [args.instrument.upper()]
    else:
        cursor.execute(
            "SELECT DISTINCT instrument FROM dbo.raw_prices ORDER BY instrument"
        )
        instruments = [r[0] for r in cursor.fetchall()]

    print(f"\nInstruments: {instruments}")

    if args.clear:
        print("\nClearing signals...")
        clear_signals(conn, args.instrument.upper() if args.instrument else None)

    # Process each instrument
    total = 0

    for instrument in instruments:
        print("\n" + "-" * 50)
        print(f"Processing: {instrument}")

        # Verify all 3 timeframes exist
        cursor.execute("""
            SELECT timeframe, COUNT(*) AS bars
            FROM dbo.raw_prices
            WHERE instrument = ?
            GROUP BY timeframe
        """, instrument)
        tf_rows = {r[0]: r[1] for r in cursor.fetchall()}
        missing = [tf for tf in ["5M","1H","4H"] if tf not in tf_rows]
        if missing:
            print(f"  [SKIP] Missing timeframes: {missing}")
            continue

        print(f"  Bars: 5M={tf_rows.get('5M',0):,}  "
              f"1H={tf_rows.get('1H',0):,}  "
              f"4H={tf_rows.get('4H',0):,}")

        # Load data
        print("  Loading price data...")
        df_5m = load_prices(conn, instrument, "5M")
        df_1h = load_prices(conn, instrument, "1H")
        df_4h = load_prices(conn, instrument, "4H")

        # Calculate Previous Day High/Low from 1H data
        print("  Calculating PDH/PDL...")
        pdhl_df = calc_prev_day_hl(df_1h)

        # Run detection
        print("  Running signal detection...")
        count = detect_signals(
            instrument, df_5m, df_1h, df_4h,
            pdhl_df, news_times, conn
        )
        print(f"  Signals detected: {count}")
        total += count

    # Summary
    print(f"\n{'='*60}")
    print("SIGNAL GENERATION COMPLETE")
    print(f"  Total signals: {total:,}")
    print("="*60)

    # Verification
    print("\nVerification — dbo.signals_mart:\n")
    cursor.execute("""
        SELECT
            instrument, direction, signal_type, signal_quality,
            COUNT(*)                             AS total,
            SUM(CAST(signal_valid AS INT))       AS valid,
            SUM(CAST(timeframes_aligned AS INT)) AS aligned,
            SUM(CAST(prior_sweep AS INT))        AS sweeps,
            MIN(entry_time)                      AS earliest,
            MAX(entry_time)                      AS latest
        FROM dbo.signals_mart
        GROUP BY instrument, direction, signal_type, signal_quality
        ORDER BY instrument, signal_type, direction
    """)
    rows = cursor.fetchall()

    if rows:
        print(f"  {'Inst':<6} {'Dir':<6} {'Type':<13} {'Q':<3} "
              f"{'Tot':>4} {'Vld':>4} {'Algn':>4} {'Swp':>4}  "
              f"{'Earliest':<12} {'Latest'}")
        print("  " + "-"*85)
        for r in rows:
            print(
                f"  {str(r[0]):<6} {str(r[1]):<6} {str(r[2]):<13} {str(r[3]):<3} "
                f"{r[4]:>4} {r[5]:>4} {r[6]:>4} {r[7]:>4}  "
                f"{str(r[8])[:10]:<12} {str(r[9])[:10]}"
            )
    else:
        print("  No signals found.")
        print("  Possible reasons:")
        print("  - 5M data window too short (only ~2 weeks)")
        print("  - PDH/PDL proximity filter too tight")
        print("  - No 4H direction bars aligned with 1H BoS in data window")

    conn.close()
    print("\nDone.\n")


if __name__ == "__main__":
    main()
