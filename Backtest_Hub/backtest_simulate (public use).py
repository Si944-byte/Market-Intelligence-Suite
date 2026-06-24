"""
backtest_simulate (public use).py
===================================
BacktestRegime — Trade Simulator (v4)
Database: BacktestRegime
Tables:   dbo.trade_mart, dbo.equity_curve_daily

Purpose:
    Simulates trade outcomes for all 4 risk tiers simultaneously.
    Each signal is simulated once per tier with tier-appropriate
    contract sizes and stop distances.
    Produces four equity curves for direct comparison in Power BI
    using both cumulative R and cumulative dollar PnL.

Risk Tier Framework:
    Tier 1 -- $0-50K    -- $300 max risk  -- baseline contracts
    Tier 2 -- $50-100K  -- $500 max risk  -- scaled contracts
    Tier 3 -- $100-200K -- $750 max risk  -- scaled contracts
    Tier 4 -- $200K+    -- $1,000 max risk -- scaled contracts

Contract Sizing Per Tier:
    Instrument Group        T1  T2  T3  T4
    MES1/MNQ1/MYM1           3   5   8   10
    MGC1/MCL1/6E1/ZC1/ZS1    2   3   5    6
    SIL1/ZN1/ZB1             1   2   3    4
    CL1/NG1                  3   5   8   10

Stop Calculation:
    stop_ticks = min(150, max_risk / (tick_value x contracts))
    Target 1   = entry +/- stop_distance x 1.5
    Target 2   = entry +/- stop_distance x 3.0

Exit Rules (all tiers):
    T1 hit  : partial exit, stop moves to breakeven for ALL sizes
    T2 hit  : runner exits at 3R
    Trailing: 2-candle trail after T1 (only moves in favor)
    Session : force exit 15:58 ET
    No entries within 30 bars of session close

Usage:
    python "backtest_simulate (public use).py"
    python "backtest_simulate (public use).py" --instrument MES1
    python "backtest_simulate (public use).py" --clear

Dependencies:
    pip install pandas pyodbc numpy
"""

import sys
import argparse
import numpy as np
import pandas as pd
import pyodbc
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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

MAX_TRADE_BARS = 300
MAX_STOP_TICKS = 150
NO_ENTRY_BARS  = 30
TRAIL_CANDLES  = 2
_ET = ZoneInfo("America/New_York")

# ── Tick sizes (points per tick) ─────────────────────────────────────────────
TICK_SIZES = {
    "MES1": 0.25,     "MNQ1": 0.25,     "MYM1": 1.0,
    "MGC1": 0.10,     "MCL1": 0.01,     "SIL1": 0.005,
    "CL1":  0.01,     "NG1":  0.001,    "6E1":  0.00005,
    "ZN1":  0.015625, "ZB1":  0.03125,
    "ZC1":  0.25,     "ZS1":  0.25,
}

# ── Dollar value per point per contract ──────────────────────────────────────
POINT_VALUES = {
    "MES1": 5.0,     "MNQ1": 2.0,      "MYM1": 0.5,
    "MGC1": 10.0,    "MCL1": 100.0,    "SIL1": 1000.0,
    "CL1":  1000.0,  "NG1":  10000.0,  "6E1":  12500.0,
    "ZN1":  1000.0,  "ZB1":  1000.0,
    "ZC1":  50.0,    "ZS1":  50.0,
}

# ============================================================================
# RISK TIER DEFINITIONS
# ============================================================================

RISK_TIERS = {
    1: {
        "label":     "Tier 1 — $0-50K",
        "max_risk":  300.0,
        "contracts": {
            "MES1": 3,  "MNQ1": 3,  "MYM1": 3,
            "MGC1": 2,  "MCL1": 2,  "6E1":  2,
            "ZC1":  2,  "ZS1":  2,
            "SIL1": 1,  "ZN1":  1,  "ZB1":  1,
            "CL1":  3,  "NG1":  3,
        }
    },
    2: {
        "label":     "Tier 2 — $50-100K",
        "max_risk":  500.0,
        "contracts": {
            "MES1": 5,  "MNQ1": 5,  "MYM1": 5,
            "MGC1": 3,  "MCL1": 3,  "6E1":  3,
            "ZC1":  3,  "ZS1":  3,
            "SIL1": 2,  "ZN1":  2,  "ZB1":  2,
            "CL1":  5,  "NG1":  5,
        }
    },
    3: {
        "label":     "Tier 3 — $100-200K",
        "max_risk":  750.0,
        "contracts": {
            "MES1": 8,  "MNQ1": 8,  "MYM1": 8,
            "MGC1": 5,  "MCL1": 5,  "6E1":  5,
            "ZC1":  5,  "ZS1":  5,
            "SIL1": 3,  "ZN1":  3,  "ZB1":  3,
            "CL1":  8,  "NG1":  8,
        }
    },
    4: {
        "label":     "Tier 4 — $200K+",
        "max_risk":  1000.0,
        "contracts": {
            "MES1": 10, "MNQ1": 10, "MYM1": 10,
            "MGC1": 6,  "MCL1": 6,  "6E1":  6,
            "ZC1":  6,  "ZS1":  6,
            "SIL1": 4,  "ZN1":  4,  "ZB1":  4,
            "CL1":  10, "NG1":  10,
        }
    },
}


# ============================================================================
# SESSION HELPERS
# ============================================================================

def utc_to_et(dt):
    return dt.replace(tzinfo=timezone.utc).astimezone(_ET)


def is_in_session(bar_time_utc):
    et      = utc_to_et(bar_time_utc)
    weekday = et.weekday()
    hour    = et.hour
    if weekday == 5:                  # Saturday — never in session
        return False
    if weekday == 4 and hour >= 17:   # Friday — CME closes at 17:00 ET
        return False
    if weekday == 6:                  # Sunday — opens at 18:00 ET
        return hour >= 18
    if hour < 16:
        return True
    if 16 <= hour < 18:               # daily maintenance gap
        return False
    return True


def is_near_session_close(bar_time_utc):
    et = utc_to_et(bar_time_utc)
    total_minutes = et.hour * 60 + et.minute
    close_minutes = 16 * 60
    return (close_minutes - NO_ENTRY_BARS * 5) <= total_minutes < close_minutes


def is_force_exit_bar(bar_time_utc):
    et = utc_to_et(bar_time_utc)
    return et.hour == 15 and et.minute == 58


# ============================================================================
# STOP CALCULATION
# ============================================================================

def calc_stop_ticks(instrument, contracts, max_risk):
    tick_size   = TICK_SIZES.get(instrument, 0.25)
    point_value = POINT_VALUES.get(instrument, 5.0)
    tick_value  = tick_size * point_value
    dollar_ticks = max_risk / (tick_value * contracts)
    return min(MAX_STOP_TICKS, dollar_ticks), tick_size


# ============================================================================
# DATABASE HELPERS
# ============================================================================

def get_connection():
    try:
        return pyodbc.connect(CONNECTION_STRING, autocommit=False)
    except pyodbc.Error as e:
        print(f"\n[ERROR] Connection failed: {e}")
        sys.exit(1)


def load_signals(conn, instrument=None):
    if instrument:
        sql = """
            SELECT signal_id, instrument, direction, signal_type,
                   signal_quality, entry_time, entry_price
            FROM dbo.signals_mart
            WHERE instrument = ?
            ORDER BY entry_time ASC
        """
        df = pd.read_sql(sql, conn, params=(instrument,))
    else:
        sql = """
            SELECT signal_id, instrument, direction, signal_type,
                   signal_quality, entry_time, entry_price
            FROM dbo.signals_mart
            ORDER BY instrument, entry_time ASC
        """
        df = pd.read_sql(sql, conn)
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    return df


def load_prices_for_instrument(conn, instrument):
    sql = """
        SELECT bar_time, open_price, high_price, low_price, close_price
        FROM dbo.raw_prices
        WHERE instrument = ? AND timeframe = '5M'
        ORDER BY bar_time ASC
    """
    df = pd.read_sql(sql, conn, params=(instrument,))
    df["bar_time"] = pd.to_datetime(df["bar_time"])
    df.columns     = ["bar_time", "open", "high", "low", "close"]
    return df.reset_index(drop=True)


def clear_trades(conn, instrument=None):
    cursor = conn.cursor()
    if instrument:
        cursor.execute(
            "DELETE FROM dbo.trade_mart WHERE instrument = ?", instrument
        )
        cursor.execute(
            "DELETE FROM dbo.equity_curve_daily WHERE instrument = ?",
            instrument
        )
        cursor.execute(
            "DELETE FROM dbo.equity_curve_daily WHERE instrument = 'ALL'"
        )
    else:
        cursor.execute("DELETE FROM dbo.trade_mart")
        cursor.execute("DELETE FROM dbo.equity_curve_daily")
    conn.commit()
    print(f"  Cleared{' ' + instrument if instrument else ' all'} trades.")


def insert_trade(cursor, trade):
    sql = """
        INSERT INTO dbo.trade_mart (
            signal_id, instrument, direction,
            entry_time, entry_price, contracts,
            partial_exit_time, partial_exit_price, partial_contracts,
            runner_exit_time, runner_exit_price, runner_contracts,
            stop_hit_time, stop_hit_price,
            outcome, exit_reason,
            partial_r, runner_r, total_r,
            points_pnl, dollar_pnl,
            trade_duration_bars, trade_duration_minutes,
            max_adverse_excursion, max_favorable_excursion,
            risk_tier
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    cursor.execute(sql, (
        trade["signal_id"],           trade["instrument"],
        trade["direction"],           trade["entry_time"],
        trade["entry_price"],         trade["contracts"],
        trade["partial_exit_time"],   trade["partial_exit_price"],
        trade["partial_contracts"],   trade["runner_exit_time"],
        trade["runner_exit_price"],   trade["runner_contracts"],
        trade["stop_hit_time"],       trade["stop_hit_price"],
        trade["outcome"],             trade["exit_reason"],
        trade["partial_r"],           trade["runner_r"],
        trade["total_r"],             trade["points_pnl"],
        trade["dollar_pnl"],          trade["trade_duration_bars"],
        trade["trade_duration_minutes"],
        trade["max_adverse_excursion"],
        trade["max_favorable_excursion"],
        trade["risk_tier"],
    ))


# ============================================================================
# CORE TRADE SIMULATOR — one signal, one tier
# ============================================================================

def simulate_trade(signal, df_5m, tier_num):
    instrument  = signal["instrument"]
    direction   = signal["direction"]
    entry_price = float(signal["entry_price"])
    entry_time  = signal["entry_time"]

    tier        = RISK_TIERS[tier_num]
    contracts   = tier["contracts"].get(instrument, 1)
    max_risk    = tier["max_risk"]
    point_value = POINT_VALUES.get(instrument, 5.0)

    # Stop calculation
    stop_ticks, tick_size = calc_stop_ticks(instrument, contracts, max_risk)
    stop_distance = stop_ticks * tick_size

    if direction == "LONG":
        stop_price = entry_price - stop_distance
    else:
        stop_price = entry_price + stop_distance

    risk_points = stop_distance

    # Targets
    target1 = (
        entry_price + risk_points * 1.5 if direction == "LONG"
        else entry_price - risk_points * 1.5
    )
    target2 = (
        entry_price + risk_points * 3.0 if direction == "LONG"
        else entry_price - risk_points * 3.0
    )

    # Partial sizing
    if contracts >= 3:
        partial_contracts = contracts - 1
        runner_contracts  = 1
    elif contracts == 2:
        partial_contracts = 1
        runner_contracts  = 1
    else:
        # 1 contract — no partial, stop to BE at T1, full contract runs
        partial_contracts = 0
        runner_contracts  = 1

    # Forward bars
    future_bars = df_5m[df_5m["bar_time"] > entry_time].head(MAX_TRADE_BARS)
    if future_bars.empty:
        return None

    future_bars = future_bars.reset_index(drop=True)
    highs       = future_bars["high"].values
    lows        = future_bars["low"].values
    closes      = future_bars["close"].values
    times       = future_bars["bar_time"].values

    # State
    partial_hit        = False
    partial_exit_time  = None
    partial_exit_price = None
    runner_exit_time   = None
    runner_exit_price  = None
    runner_exit_reason = None
    stop_hit           = False
    stop_hit_time      = None
    stop_hit_price     = None
    current_stop       = stop_price
    trailing_stop      = entry_price
    max_adverse        = 0.0
    max_favorable      = 0.0
    bars_processed     = 0

    for i in range(len(future_bars)):
        bar_time_i = pd.Timestamp(times[i])
        bar_high   = float(highs[i])
        bar_low    = float(lows[i])
        bar_close  = float(closes[i])
        bars_processed += 1

        # Session boundary check
        if not is_in_session(bar_time_i):
            if partial_hit and runner_exit_time is None:
                runner_exit_time   = bar_time_i
                runner_exit_price  = bar_close
                runner_exit_reason = "SESSION_CLOSE"
            elif not partial_hit:
                stop_hit       = True
                stop_hit_time  = bar_time_i
                stop_hit_price = bar_close
            break

        # Force exit at 15:58 ET
        if is_force_exit_bar(bar_time_i):
            if partial_hit and runner_exit_time is None:
                runner_exit_time   = bar_time_i
                runner_exit_price  = bar_close
                runner_exit_reason = "SESSION_CLOSE"
            elif not partial_hit:
                stop_hit       = True
                stop_hit_time  = bar_time_i
                stop_hit_price = bar_close
            break

        # MAE / MFE tracking
        if direction == "LONG":
            max_adverse   = max(max_adverse,   entry_price - bar_low)
            max_favorable = max(max_favorable, bar_high - entry_price)
        else:
            max_adverse   = max(max_adverse,   bar_high - entry_price)
            max_favorable = max(max_favorable, entry_price - bar_low)

        # ── Phase 1: Before Target 1 ─────────────────────────────────────
        if not partial_hit:

            # Stop hit
            if direction == "LONG" and bar_low <= current_stop:
                stop_hit       = True
                stop_hit_time  = bar_time_i
                stop_hit_price = current_stop
                break
            if direction == "SHORT" and bar_high >= current_stop:
                stop_hit       = True
                stop_hit_time  = bar_time_i
                stop_hit_price = current_stop
                break

            # Target 1 hit — partial exit + stop to breakeven ALL sizes
            if direction == "LONG" and bar_high >= target1:
                partial_hit        = True
                partial_exit_time  = bar_time_i
                partial_exit_price = target1 if partial_contracts > 0 else None
                current_stop  = entry_price
                trailing_stop = entry_price

            elif direction == "SHORT" and bar_low <= target1:
                partial_hit        = True
                partial_exit_time  = bar_time_i
                partial_exit_price = target1 if partial_contracts > 0 else None
                current_stop  = entry_price
                trailing_stop = entry_price

        # ── Phase 2: Runner between T1 and T2 ───────────────────────────
        else:
            # Update trailing stop — 2 candle trail, only moves in favor
            if i >= TRAIL_CANDLES:
                if direction == "LONG":
                    new_trail     = float(lows[i - TRAIL_CANDLES])
                    trailing_stop = max(trailing_stop, new_trail)
                    current_stop  = max(current_stop,  trailing_stop)
                else:
                    new_trail     = float(highs[i - TRAIL_CANDLES])
                    trailing_stop = min(trailing_stop, new_trail)
                    current_stop  = min(current_stop,  trailing_stop)

            # Trailing stop hit
            if direction == "LONG" and bar_low <= current_stop:
                runner_exit_time   = bar_time_i
                runner_exit_price  = current_stop
                runner_exit_reason = "TRAIL_STOP"
                break
            if direction == "SHORT" and bar_high >= current_stop:
                runner_exit_time   = bar_time_i
                runner_exit_price  = current_stop
                runner_exit_reason = "TRAIL_STOP"
                break

            # Target 2 hit
            if direction == "LONG" and bar_high >= target2:
                runner_exit_time   = bar_time_i
                runner_exit_price  = target2
                runner_exit_reason = "TARGET2"
                break
            if direction == "SHORT" and bar_low <= target2:
                runner_exit_time   = bar_time_i
                runner_exit_price  = target2
                runner_exit_reason = "TARGET2"
                break

    # Resolve unfinished trades at end of bar window
    if not stop_hit and runner_exit_time is None:
        last       = future_bars.iloc[-1]
        last_price = float(last["close"])
        last_time  = last["bar_time"]
        if not partial_hit:
            stop_hit       = True
            stop_hit_time  = last_time
            stop_hit_price = last_price
        else:
            runner_exit_time   = last_time
            runner_exit_price  = last_price
            runner_exit_reason = "MAX_BARS"

    # ── P&L Calculation ──────────────────────────────────────────────────────
    partial_pnl = 0.0
    runner_pnl  = 0.0
    partial_r   = 0.0
    runner_r    = 0.0

    if partial_hit and partial_contracts > 0 and partial_exit_price:
        if direction == "LONG":
            partial_pnl = (partial_exit_price - entry_price) * partial_contracts * point_value
            partial_r   = (partial_exit_price - entry_price) / risk_points
        else:
            partial_pnl = (entry_price - partial_exit_price) * partial_contracts * point_value
            partial_r   = (entry_price - partial_exit_price) / risk_points

    if partial_hit and runner_contracts > 0 and runner_exit_price is not None:
        if direction == "LONG":
            runner_pnl = (runner_exit_price - entry_price) * runner_contracts * point_value
            runner_r   = (runner_exit_price - entry_price) / risk_points
        else:
            runner_pnl = (entry_price - runner_exit_price) * runner_contracts * point_value
            runner_r   = (entry_price - runner_exit_price) / risk_points

    if stop_hit and not partial_hit and stop_hit_price:
        if direction == "LONG":
            partial_pnl = (stop_hit_price - entry_price) * contracts * point_value
            partial_r   = (stop_hit_price - entry_price) / risk_points
        else:
            partial_pnl = (entry_price - stop_hit_price) * contracts * point_value
            partial_r   = (entry_price - stop_hit_price) / risk_points

    total_r      = partial_r + runner_r
    dollar_pnl   = partial_pnl + runner_pnl
    total_points = dollar_pnl / point_value if point_value > 0 else 0

    # ── Outcome ──────────────────────────────────────────────────────────────
    if not partial_hit:
        outcome     = "LOSS"
        exit_reason = "STOP"
    elif runner_exit_reason == "TARGET2":
        outcome     = "WIN"
        exit_reason = "TP2"
    elif runner_exit_reason == "SESSION_CLOSE":
        outcome     = "WIN" if runner_pnl > 0 else (
            "LOSS" if runner_pnl < 0 else "BREAKEVEN"
        )
        exit_reason = "SESSION_CLOSE"
    elif runner_pnl > 0:
        outcome     = "WIN"
        exit_reason = "TP1+TRAIL"
    elif runner_pnl < 0:
        outcome     = "LOSS"
        exit_reason = "TP1+LOSS"
    elif runner_contracts == 0:
        outcome     = "WIN"
        exit_reason = "TP1_ONLY"
    else:
        outcome     = "BREAKEVEN"
        exit_reason = "BE_STOP"

    # ── Duration ─────────────────────────────────────────────────────────────
    duration_bars    = bars_processed
    duration_minutes = duration_bars * 5

    return {
        "signal_id":               int(signal["signal_id"]),
        "instrument":              instrument,
        "direction":               direction,
        "entry_time":              entry_time,
        "entry_price":             entry_price,
        "contracts":               contracts,
        "partial_exit_time":       partial_exit_time,
        "partial_exit_price":      partial_exit_price,
        "partial_contracts":       partial_contracts if (partial_hit and partial_contracts > 0) else None,
        "runner_exit_time":        runner_exit_time,
        "runner_exit_price":       runner_exit_price,
        "runner_contracts":        runner_contracts if partial_hit else None,
        "stop_hit_time":           stop_hit_time if stop_hit else None,
        "stop_hit_price":          stop_hit_price if stop_hit else None,
        "outcome":                 outcome,
        "exit_reason":             exit_reason,
        "partial_r":               round(partial_r, 4),
        "runner_r":                round(runner_r, 4) if runner_contracts > 0 else None,
        "total_r":                 round(total_r, 4),
        "points_pnl":              round(total_points, 4),
        "dollar_pnl":              round(dollar_pnl, 2),
        "trade_duration_bars":     duration_bars,
        "trade_duration_minutes":  duration_minutes,
        "max_adverse_excursion":   round(max_adverse, 4),
        "max_favorable_excursion": round(max_favorable, 4),
        "risk_tier":               tier_num,
    }


# ============================================================================
# EQUITY CURVE BUILDER — all tiers, cumulative R and dollar PnL
# ============================================================================

def build_equity_curve(conn):
    cursor = conn.cursor()

    sql = """
        SELECT instrument, entry_time, total_r, points_pnl,
               dollar_pnl, outcome, risk_tier
        FROM dbo.trade_mart
        ORDER BY risk_tier, entry_time ASC
    """
    df = pd.read_sql(sql, conn)

    if df.empty:
        print("  No trades for equity curve.")
        return

    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["trade_date"] = df["entry_time"].dt.date
    df["is_win"]     = (df["outcome"] == "WIN").astype(int)
    df["is_loss"]    = (df["outcome"] == "LOSS").astype(int)

    rows_inserted = 0

    for tier_num in RISK_TIERS.keys():
        df_tier = df[df["risk_tier"] == tier_num].copy()
        if df_tier.empty:
            continue

        instruments = list(df_tier["instrument"].unique()) + ["ALL"]

        for instrument in instruments:
            df_inst = (
                df_tier if instrument == "ALL"
                else df_tier[df_tier["instrument"] == instrument].copy()
            )
            if df_inst.empty:
                continue

            # Daily aggregation including dollar PnL
            daily = df_inst.groupby("trade_date").agg(
                trades_count    =("total_r",    "count"),
                wins            =("is_win",     "sum"),
                losses          =("is_loss",    "sum"),
                daily_r         =("total_r",    "sum"),
                daily_points    =("points_pnl", "sum"),
                daily_dollar_pnl=("dollar_pnl", "sum"),
            ).reset_index().sort_values("trade_date").reset_index(drop=True)

            # Cumulative columns
            daily["cumulative_r"]          = daily["daily_r"].cumsum()
            daily["cumulative_points"]     = daily["daily_points"].cumsum()
            daily["cumulative_dollar_pnl"] = daily["daily_dollar_pnl"].cumsum()

            # Drawdown from peak cumulative R
            cum_r     = daily["cumulative_r"].values
            peak      = np.maximum.accumulate(cum_r)
            drawdowns = cum_r - peak

            for idx, row in daily.iterrows():
                trades_to_date = df_inst[df_inst["trade_date"] <= row["trade_date"]]
                win_rate_20    = round(
                    float(trades_to_date.tail(20)["is_win"].mean()), 4
                )

                try:
                    cursor.execute("""
                        IF NOT EXISTS (
                            SELECT 1 FROM dbo.equity_curve_daily
                            WHERE curve_date = ? AND instrument = ? AND risk_tier = ?
                        )
                        INSERT INTO dbo.equity_curve_daily (
                            curve_date, instrument, risk_tier,
                            trades_count, wins, losses,
                            daily_r, cumulative_r,
                            daily_points, cumulative_points,
                            win_rate_rolling_20, drawdown_r,
                            cumulative_dollar_pnl
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        row["trade_date"], instrument, tier_num,
                        row["trade_date"], instrument, tier_num,
                        int(row["trades_count"]),
                        int(row["wins"]),
                        int(row["losses"]),
                        round(float(row["daily_r"]), 4),
                        round(float(daily["cumulative_r"].iloc[idx]), 4),
                        round(float(row["daily_points"]), 4),
                        round(float(daily["cumulative_points"].iloc[idx]), 4),
                        win_rate_20,
                        round(float(drawdowns[idx]), 4),
                        round(float(daily["cumulative_dollar_pnl"].iloc[idx]), 2),
                    ))
                    rows_inserted += 1
                except pyodbc.Error as e:
                    print(f"  [ERROR] Equity curve insert: {e}")

    conn.commit()
    print(f"  Equity curve rows inserted: {rows_inserted:,}")


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="BacktestRegime Trade Simulator v4 — Multi-Tier"
    )
    parser.add_argument("--instrument", type=str, default=None)
    parser.add_argument("--clear",      action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("BacktestRegime — Trade Simulator v4 (Multi-Tier)")
    print(f"Server : {SQL_SERVER}  |  DB: {SQL_DATABASE}")
    print(f"Tiers  : {len(RISK_TIERS)} risk tiers simulated simultaneously")
    print("=" * 60)

    conn = get_connection()
    print("  Connected.")

    if args.clear:
        print("\nClearing existing trades...")
        clear_trades(conn, args.instrument.upper() if args.instrument else None)

    # Load signals
    print("\nLoading signals...")
    instrument_filter = args.instrument.upper() if args.instrument else None
    signals = load_signals(conn, instrument_filter)
    print(f"  {len(signals):,} signals loaded.")

    if signals.empty:
        print("  No signals found. Run backtest_signals.py first.")
        conn.close()
        sys.exit(0)

    # Session filter
    print("\nApplying session filter...")
    before  = len(signals)
    signals = signals[signals["entry_time"].apply(
        lambda t: is_in_session(t) and not is_near_session_close(t)
    )].reset_index(drop=True)
    after   = len(signals)
    print(f"  After session filter: {after:,} (removed {before - after:,})")

    # Process
    cursor             = conn.cursor()
    total_trades       = 0
    current_instrument = None
    df_5m              = None
    tier_stats         = {t: {"wins": 0, "losses": 0, "be": 0} for t in RISK_TIERS}

    for _, signal in signals.iterrows():
        instrument = signal["instrument"]

        if instrument != current_instrument:
            print(f"\n{'-'*55}")
            print(f"Simulating: {instrument}")
            df_5m = load_prices_for_instrument(conn, instrument)
            print(f"  5M bars: {len(df_5m):,}")
            current_instrument = instrument

            # Show stop per tier for this instrument
            for tier_num, tier in RISK_TIERS.items():
                contracts  = tier["contracts"].get(instrument, 1)
                st, ts     = calc_stop_ticks(instrument, contracts, tier["max_risk"])
                stop_dol   = st * ts * POINT_VALUES.get(instrument, 5.0) * contracts
                print(f"  T{tier_num}: {contracts}ct | "
                      f"Stop {st:.1f} ticks = ${stop_dol:.0f}")

        # Simulate all 4 tiers for this signal
        for tier_num in RISK_TIERS.keys():
            trade = simulate_trade(signal, df_5m, tier_num)
            if trade is None:
                continue
            try:
                insert_trade(cursor, trade)
                conn.commit()
                total_trades += 1
                if trade["outcome"] == "WIN":
                    tier_stats[tier_num]["wins"] += 1
                elif trade["outcome"] == "LOSS":
                    tier_stats[tier_num]["losses"] += 1
                else:
                    tier_stats[tier_num]["be"] += 1
            except pyodbc.Error as e:
                print(f"  [ERROR] Insert failed signal "
                      f"{signal['signal_id']} tier {tier_num}: {e}")

    # Build equity curves
    print(f"\n{'-'*55}")
    print("Building equity curves (all tiers)...")
    build_equity_curve(conn)

    # Summary
    print(f"\n{'='*60}")
    print("SIMULATION COMPLETE")
    print(f"  Total rows: {total_trades:,} ({len(signals):,} signals x 4 tiers)")
    print()
    for tier_num, tier in RISK_TIERS.items():
        s        = tier_stats[tier_num]
        total_t  = s["wins"] + s["losses"] + s["be"]
        wr       = s["wins"] / total_t * 100 if total_t > 0 else 0
        print(f"  T{tier_num} {tier['label']:<24} "
              f"W:{s['wins']:>4} L:{s['losses']:>4} "
              f"BE:{s['be']:>3} WR:{wr:.1f}%")
    print("="*60)

    # Verification by tier
    print("\nVerification — dbo.trade_mart by tier:\n")
    cursor.execute("""
        SELECT
            risk_tier,
            COUNT(*)                                           AS trades,
            SUM(CASE WHEN outcome='WIN'  THEN 1 ELSE 0 END)   AS wins,
            SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END)   AS losses,
            CAST(SUM(CASE WHEN outcome='WIN' THEN 1.0 ELSE 0 END)
                / COUNT(*) * 100 AS DECIMAL(5,1))             AS win_pct,
            CAST(AVG(total_r)    AS DECIMAL(6,3))             AS avg_r,
            CAST(SUM(total_r)    AS DECIMAL(8,1))             AS total_r,
            CAST(SUM(dollar_pnl) AS DECIMAL(12,0))            AS dollar_pnl
        FROM dbo.trade_mart
        GROUP BY risk_tier
        ORDER BY risk_tier
    """)
    rows = cursor.fetchall()
    if rows:
        print(f"  {'Tier':<6} {'Trades':>6} {'W':>5} {'L':>5} "
              f"{'Win%':>6} {'AvgR':>6} {'TotR':>8} {'$PnL':>12}")
        print("  " + "-"*65)
        for r in rows:
            print(f"  T{r[0]:<5} {r[1]:>6} {r[2]:>5} {r[3]:>5} "
                  f"{str(r[4]):>6}% {str(r[5]):>6} "
                  f"{str(r[6]):>8} ${str(r[7]):>11}")

    # Equity curve summary
    print("\nVerification — equity_curve_daily peak by tier:\n")
    cursor.execute("""
        SELECT
            risk_tier,
            MAX(cumulative_r)          AS peak_r,
            MAX(cumulative_dollar_pnl) AS peak_dollar,
            MIN(drawdown_r)            AS max_dd,
            MAX(curve_date)            AS latest
        FROM dbo.equity_curve_daily
        WHERE instrument = 'ALL'
        GROUP BY risk_tier
        ORDER BY risk_tier
    """)
    rows = cursor.fetchall()
    if rows:
        print(f"  {'Tier':<6} {'Peak R':>10} {'Peak $':>12} "
              f"{'Max DD':>8} {'Latest'}")
        print("  " + "-"*55)
        for r in rows:
            print(f"  T{r[0]:<5} {str(r[1]):>10} "
                  f"${str(r[2]):>11} {str(r[3]):>8} {str(r[4])[:10]}")

    conn.close()
    print("\nDone.\n")


if __name__ == "__main__":
    main()
