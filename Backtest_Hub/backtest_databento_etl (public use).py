"""
backtest_databento_etl (public use).py
=======================================
BacktestRegime — Step 1: Databento Price ETL
Database: BacktestRegime
Table:    dbo.raw_prices

Purpose:
    Pulls OHLCV futures data from Databento for all 13 instruments.
    Resamples 1M bars to 5M, 1H, and 4H.
    Saves CSV backups to disk.
    Loads into dbo.raw_prices in SQL Server.

    Two modes:
    FULL  — pulls complete history from START_DATE to today (first run)
    DELTA — pulls last 7 days only and appends (weekly scheduled runs)

    Mode is determined automatically:
    - If instrument has NO data in raw_prices -> FULL pull
    - If instrument HAS data in raw_prices    -> DELTA pull (last 7 days)

Usage:
    python "backtest_databento_etl (public use).py"                        (auto mode)
    python "backtest_databento_etl (public use).py" --full                 (force full pull)
    python "backtest_databento_etl (public use).py" --instrument MES       (single instrument)

Dependencies:
    pip install databento pandas pyodbc python-dotenv
"""

import os
import sys
import argparse
import pyodbc
import databento as db
import pandas as pd
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# CONFIGURATION — set these in your .env file
# ============================================================================
DATABENTO_API_KEY = os.environ.get("DATABENTO_API_KEY", "YOUR_DATABENTO_API_KEY")
SQL_SERVER        = os.environ.get("SQL_SERVER",        "YOUR_SQL_SERVER")
SQL_USER          = os.environ.get("SQL_USER",          "YOUR_SQL_USER")
SQL_PASSWORD      = os.environ.get("SQL_PASSWORD",      "YOUR_SQL_PASSWORD")
BACKUP_FOLDER     = os.environ.get("DATABENTO_BACKUP_FOLDER", r".\Data\Databento")

DATABENTO_DATASET = "GLBX.MDP3"           # CME, CBOT, NYMEX, COMEX all instruments

# Historical start date — 5 years of history
FULL_START_DATE = "2021-01-01"

# SQL Server connection
SQL_DATABASE      = "BacktestRegime"
CONNECTION_STRING = (
    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
    f"SERVER={SQL_SERVER};"
    f"DATABASE=BacktestRegime;"
    f"UID={SQL_USER};"
    f"PWD={SQL_PASSWORD};"
    f"TrustServerCertificate=yes;"
)

# Batch size for SQL inserts
BATCH_SIZE = 500

# ============================================================================
# INSTRUMENT MASTER
# Databento continuous symbol -> our instrument code in raw_prices
# Format: ROOT.c.0 = front month continuous contract
# ============================================================================

INSTRUMENTS = {
    "MES.c.0": "MES1",
    "MNQ.c.0": "MNQ1",
    "MYM.c.0": "MYM1",
    "MGC.c.0": "MGC1",
    "MCL.c.0": "MCL1",
    "SIL.c.0": "SIL1",
    "CL.c.0":  "CL1",
    "NG.c.0":  "NG1",
    "6E.c.0":  "6E1",
    "ZN.c.0":  "ZN1",
    "ZB.c.0":  "ZB1",
    "ZC.c.0":  "ZC1",
    "ZS.c.0":  "ZS1",
}

# ============================================================================
# TIMEFRAMES TO GENERATE FROM 1M DATA
# ============================================================================

TIMEFRAMES = {
    "5M": "5min",
    "1H": "1h",
    "4H": "4h",
}


# ============================================================================
# DATABASE HELPERS
# ============================================================================

def get_connection():
    try:
        return pyodbc.connect(CONNECTION_STRING, autocommit=False)
    except pyodbc.Error as e:
        print(f"\n[ERROR] SQL Server connection failed: {e}")
        sys.exit(1)


def get_latest_bar_time(conn, instrument):
    """
    Returns the latest bar_time for an instrument in raw_prices.
    Returns None if no data exists (triggers full pull).
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT MAX(bar_time)
        FROM dbo.raw_prices
        WHERE instrument = ?
    """, instrument)
    row = cursor.fetchone()
    if row and row[0]:
        return pd.Timestamp(row[0])
    return None


def insert_bars(conn, df, instrument, timeframe):
    """
    Insert resampled bars into dbo.raw_prices.
    Skips duplicates on re-run (idempotent).
    Returns (inserted, skipped) counts.
    """
    cursor   = conn.cursor()
    inserted = 0
    skipped  = 0
    rows     = df.values.tolist()
    total    = len(rows)

    sql = """
        IF NOT EXISTS (
            SELECT 1 FROM dbo.raw_prices
            WHERE instrument = ? AND timeframe = ? AND bar_time = ?
        )
        INSERT INTO dbo.raw_prices
            (instrument, timeframe, bar_time,
             open_price, high_price, low_price, close_price, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    for i in range(0, total, BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        for row in batch:
            bar_time, o, h, l, c, v = row
            try:
                cursor.execute(sql, (
                    instrument, timeframe, bar_time,
                    instrument, timeframe, bar_time,
                    float(o), float(h), float(l), float(c), int(v)
                ))
                if cursor.rowcount == 1:
                    inserted += 1
                else:
                    skipped += 1
            except pyodbc.Error as e:
                print(f"\n  [ERROR] Insert failed: {e}")
                skipped += 1

        conn.commit()
        pct = min((i + BATCH_SIZE) / total * 100, 100)
        print(f"  {timeframe} insert: {pct:.0f}% ({min(i+BATCH_SIZE,total):,}/{total:,})", end="\r")

    print()
    return inserted, skipped


# ============================================================================
# CSV BACKUP HELPERS
# ============================================================================

def ensure_backup_folder():
    """Create backup folder if it doesn't exist."""
    if not os.path.exists(BACKUP_FOLDER):
        os.makedirs(BACKUP_FOLDER)
        print(f"  Created backup folder: {BACKUP_FOLDER}")


def get_backup_path(instrument_code):
    """Returns the CSV backup file path for a given instrument."""
    return os.path.join(BACKUP_FOLDER, f"{instrument_code}_1m.csv")


def save_csv_backup(df_1m, instrument_code):
    """
    Save or append 1M bars to CSV backup.
    If file exists, appends new rows and deduplicates.
    """
    path = get_backup_path(instrument_code)

    if os.path.exists(path):
        existing = pd.read_csv(path, index_col=0, parse_dates=True)
        combined = pd.concat([existing, df_1m])
        combined = combined[~combined.index.duplicated(keep='last')]
        combined = combined.sort_index()
        combined.to_csv(path)
        print(f"  CSV backup updated: {os.path.basename(path)} ({len(combined):,} rows)")
    else:
        df_1m.to_csv(path)
        print(f"  CSV backup created: {os.path.basename(path)} ({len(df_1m):,} rows)")


def load_from_csv_backup(instrument_code):
    """
    Load 1M bars from CSV backup.
    Used as fallback if Databento is unavailable.
    """
    path = get_backup_path(instrument_code)
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    print(f"  Loaded from CSV backup: {os.path.basename(path)} ({len(df):,} rows)")
    return df


# ============================================================================
# DATABENTO PULL
# ============================================================================

def pull_from_databento(client, symbol, start_date, end_date):
    """
    Pull 1M OHLCV bars from Databento for a single symbol.
    Returns DataFrame with DatetimeIndex (UTC) and columns:
        open, high, low, close, volume
    """
    print(f"  Pulling from Databento: {symbol} | {start_date} -> {end_date}")

    data = client.timeseries.get_range(
        dataset=DATABENTO_DATASET,
        schema="ohlcv-1m",
        start=start_date,
        end=end_date,
        symbols=[symbol],
        stype_in="continuous",
    )

    df = data.to_df()

    if df.empty:
        print(f"  [WARN] No data returned for {symbol}")
        return None

    df = df[["open", "high", "low", "close", "volume"]].copy()

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df.index = df.index.tz_localize(None)
    df.index.name = "bar_time"

    print(f"  Rows received: {len(df):,}")
    print(f"  Date range   : {df.index.min()} -> {df.index.max()}")

    return df


# ============================================================================
# RESAMPLE 1M TO HIGHER TIMEFRAMES
# ============================================================================

def resample_ohlcv(df_1m, timeframe_str):
    """
    Resample 1M DataFrame to a higher timeframe.
    timeframe_str: pandas offset string e.g. '5min', '1h', '4h'
    """
    df_resampled = df_1m.resample(timeframe_str, label='left', closed='left').agg({
        'open':   'first',
        'high':   'max',
        'low':    'min',
        'close':  'last',
        'volume': 'sum',
    }).dropna(subset=['open', 'close'])

    return df_resampled


# ============================================================================
# PROCESS ONE INSTRUMENT
# ============================================================================

def process_instrument(client, symbol, instrument_code, conn,
                        force_full=False):
    """
    Full pipeline for one instrument:
    1. Determine pull mode (FULL or DELTA)
    2. Pull from Databento
    3. Save CSV backup
    4. Resample to 5M, 1H, 4H
    5. Insert into raw_prices
    """
    print(f"\n{'-'*55}")
    print(f"Instrument : {instrument_code} ({symbol})")

    latest = get_latest_bar_time(conn, instrument_code)
    today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if force_full or latest is None:
        mode       = "FULL"
        start_date = FULL_START_DATE
    else:
        mode       = "DELTA"
        delta_start = (latest - timedelta(days=7)).strftime("%Y-%m-%d")
        start_date  = delta_start

    end_date = today
    print(f"Mode       : {mode}")
    print(f"Pull range : {start_date} -> {end_date}")

    try:
        df_1m = pull_from_databento(client, symbol, start_date, end_date)
    except Exception as e:
        print(f"  [ERROR] Databento pull failed: {e}")
        print(f"  Attempting CSV backup fallback...")
        df_1m = load_from_csv_backup(instrument_code)
        if df_1m is None:
            print(f"  [ERROR] No CSV backup available. Skipping {instrument_code}.")
            return

    if df_1m is None or df_1m.empty:
        print(f"  [SKIP] No data returned for {instrument_code}")
        return

    print(f"  Saving CSV backup...")
    save_csv_backup(df_1m, instrument_code)

    total_inserted = 0
    total_skipped  = 0

    for tf_label, tf_pandas in TIMEFRAMES.items():
        print(f"  Resampling to {tf_label}...")
        df_tf = resample_ohlcv(df_1m, tf_pandas)

        if df_tf.empty:
            print(f"  [WARN] No {tf_label} bars generated")
            continue

        print(f"  {tf_label} bars: {len(df_tf):,} | "
              f"{df_tf.index.min()} -> {df_tf.index.max()}")

        df_insert = df_tf.reset_index()[
            ["bar_time", "open", "high", "low", "close", "volume"]
        ]

        ins, skp = insert_bars(conn, df_insert, instrument_code, tf_label)
        total_inserted += ins
        total_skipped  += skp
        print(f"  {tf_label} inserted: {ins:,} | skipped: {skp:,}")

    print(f"  Total inserted: {total_inserted:,} | Total skipped: {total_skipped:,}")
    return total_inserted


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="BacktestRegime Databento Price ETL"
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Force full historical pull regardless of existing data"
    )
    parser.add_argument(
        "--instrument", type=str, default=None,
        help="Pull single instrument only e.g. --instrument MES"
    )
    args = parser.parse_args()

    print("=" * 55)
    print("BacktestRegime - Databento Price ETL")
    print(f"Server  : {SQL_SERVER}  |  DB: {SQL_DATABASE}")
    print(f"Dataset : {DATABENTO_DATASET}")
    print(f"Mode    : {'FULL (forced)' if args.full else 'AUTO (full or delta)'}")
    print("=" * 55)

    if DATABENTO_API_KEY == "YOUR_DATABENTO_API_KEY":
        print("\n[ERROR] Please set your Databento API key.")
        print("  Add DATABENTO_API_KEY=your_key to your .env file.")
        sys.exit(1)

    ensure_backup_folder()

    print("\nConnecting to Databento...")
    try:
        client = db.Historical(DATABENTO_API_KEY)
        print("  Connected.")
    except Exception as e:
        print(f"\n[ERROR] Databento connection failed: {e}")
        sys.exit(1)

    print("\nConnecting to SQL Server...")
    conn = get_connection()
    print("  Connected.")

    if args.instrument:
        symbol = f"{args.instrument.upper()}.c.0"
        if symbol not in INSTRUMENTS:
            print(f"\n[ERROR] Unknown instrument: {args.instrument}")
            print(f"  Available: {list(INSTRUMENTS.keys())}")
            sys.exit(1)
        instruments_to_process = {symbol: INSTRUMENTS[symbol]}
    else:
        instruments_to_process = INSTRUMENTS

    print(f"\nInstruments to process: {len(instruments_to_process)}")

    if args.full:
        print("\nChecking estimated cost...")
        try:
            symbols_list = list(instruments_to_process.keys())
            cost = client.metadata.get_cost(
                dataset=DATABENTO_DATASET,
                schema="ohlcv-1m",
                start=FULL_START_DATE,
                end=datetime.now().strftime("%Y-%m-%d"),
                symbols=symbols_list,
                stype_in="continuous",
            )
            print(f"  Estimated cost: ${cost:.2f}")
            if cost > 125:
                print(f"  [WARN] Cost exceeds $125 free credit threshold")
                confirm = input("  Continue? (y/n): ")
                if confirm.lower() != 'y':
                    print("  Aborted.")
                    sys.exit(0)
        except Exception as e:
            print(f"  [WARN] Could not get cost estimate: {e}")

    total_inserted = 0
    errors         = []

    for symbol, instrument_code in instruments_to_process.items():
        try:
            inserted = process_instrument(
                client, symbol, instrument_code, conn,
                force_full=args.full
            )
            if inserted:
                total_inserted += inserted
        except Exception as e:
            print(f"\n  [ERROR] Failed to process {instrument_code}: {e}")
            errors.append(instrument_code)
            continue

    print(f"\n{'='*55}")
    print("DATABENTO ETL COMPLETE")
    print(f"  Total bars inserted : {total_inserted:,}")
    print(f"  Errors              : {len(errors)}")
    if errors:
        print(f"  Failed instruments  : {errors}")
    print("="*55)

    print("\nVerification - dbo.raw_prices row counts:\n")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            instrument,
            timeframe,
            COUNT(*)        AS bar_count,
            MIN(bar_time)   AS earliest,
            MAX(bar_time)   AS latest
        FROM dbo.raw_prices
        GROUP BY instrument, timeframe
        ORDER BY instrument, timeframe
    """)
    rows = cursor.fetchall()
    if rows:
        print(f"  {'Instrument':<8} {'TF':<5} {'Bars':>8}  "
              f"{'Earliest':<22} {'Latest'}")
        print("  " + "-"*65)
        for r in rows:
            print(f"  {r[0]:<8} {r[1]:<5} {r[2]:>8,}  "
                  f"{str(r[3]):<22} {str(r[4])}")
    else:
        print("  No data found.")

    conn.close()
    print("\nDone.\n")


if __name__ == "__main__":
    main()
