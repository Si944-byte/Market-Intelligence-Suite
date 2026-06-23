"""
backtest_price_etl (public use).py
===================================
BacktestRegime — Step 1: Price Data ETL
Database: BacktestRegime
Table:    dbo.raw_prices

Purpose:
    Reads TradingView CSV exports from the Raw data folder.
    Strips all indicator columns — keeps OHLCV only.
    Normalizes timeframe labels (5m -> 5M, 1H -> 1H, 4H -> 4H).
    Converts timestamps to UTC.
    Upserts into dbo.raw_prices.
    Skips duplicates on re-run safely.

File naming convention:
    MES1_1H.csv, MES1_4H.csv, MES1_5m.csv
    MGC1_1H.csv, MGC1_4H.csv, MGC1_5m.csv
    MNQ1_1H.csv, MNQ1_4H.csv, MNQ1_5m.csv
    (add remaining instruments to RAW_DATA_FOLDER when ready)

Usage:
    python "backtest_price_etl (public use).py"

Dependencies:
    pip install pandas pyodbc python-dateutil
"""

import os
import glob
import sys
import pandas as pd
import pyodbc
from datetime import timezone
from dateutil import parser as dateutil_parser

# ============================================================================
# CONFIGURATION — update these values for your environment
# ============================================================================
# Recommended: store credentials in a .env file and load with python-dotenv
# See README.md for setup instructions

RAW_DATA_FOLDER = r"YOUR_RAW_DATA_FOLDER"

SQL_SERVER        = "YOUR_SQL_SERVER"
SQL_DATABASE      = "BacktestRegime"
CONNECTION_STRING = (
    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
    f"SERVER={SQL_SERVER};"
    f"DATABASE={SQL_DATABASE};"
    f"UID=YOUR_SQL_USER;PWD=YOUR_SQL_PASSWORD;"
)

# Batch size for SQL inserts — keeps memory manageable on large 5M files
BATCH_SIZE = 500

# ============================================================================
# TIMEFRAME NORMALIZER
# Handles mixed case from TradingView exports
# ============================================================================

TIMEFRAME_MAP = {
    "5m": "5M",
    "5M": "5M",
    "1h": "1H",
    "1H": "1H",
    "4h": "4H",
    "4H": "4H",
}

# ============================================================================
# COLUMNS WE KEEP — everything else gets dropped
# ============================================================================

OHLCV_COLUMNS = ["time", "open", "high", "low", "close", "Volume"]


# ============================================================================
# HELPER: Parse filename -> (instrument, timeframe)
# "MES1_1H.csv"  -> ("MES1", "1H")
# "MGC1_5m.csv"  -> ("MGC1", "5M")
# ============================================================================

def parse_filename(filepath):
    basename     = os.path.basename(filepath)
    name_no_ext  = os.path.splitext(basename)[0]
    parts        = name_no_ext.split("_")

    if len(parts) != 2:
        print(f"  [SKIP] Unrecognized filename format: {basename}")
        return None, None

    instrument = parts[0].upper()
    tf_raw     = parts[1]
    timeframe  = TIMEFRAME_MAP.get(tf_raw)

    if timeframe is None:
        print(f"  [SKIP] Unrecognized timeframe '{tf_raw}' in: {basename}")
        return None, None

    return instrument, timeframe


# ============================================================================
# HELPER: Parse ISO 8601 timestamp with timezone offset -> UTC naive datetime
# TradingView format: "2026-04-07T02:00:00-04:00"
# Stored as UTC:      "2026-04-07 06:00:00"
# ============================================================================

def parse_to_utc(ts_string):
    try:
        dt = dateutil_parser.parse(str(ts_string))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


# ============================================================================
# HELPER: Load and clean a single CSV file
# ============================================================================

def load_csv(filepath, instrument, timeframe):
    print(f"  Loading : {os.path.basename(filepath)}")

    try:
        df = pd.read_csv(filepath)
    except Exception as e:
        print(f"  [ERROR] Could not read file: {e}")
        return None

    # Strip whitespace from column names
    df.columns = [c.strip() for c in df.columns]

    # Case-insensitive column matching for robustness
    col_lower_map = {c.lower(): c for c in df.columns}
    rename = {}
    for required in OHLCV_COLUMNS:
        if required not in df.columns:
            if required.lower() in col_lower_map:
                rename[col_lower_map[required.lower()]] = required

    if rename:
        df = df.rename(columns=rename)

    # Check all required columns are now present
    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        print(f"  [ERROR] Missing columns: {missing}")
        print(f"          Available columns: {list(df.columns)}")
        return None

    # Keep OHLCV only — drop all indicator columns
    df = df[OHLCV_COLUMNS].copy()

    # Rename Volume -> volume for SQL consistency
    df = df.rename(columns={"Volume": "volume"})

    # Parse timestamps to UTC
    print(f"  Parsing {len(df):,} rows to UTC...")
    df["bar_time"] = df["time"].apply(parse_to_utc)

    # Report and drop failed timestamp rows
    null_count = df["bar_time"].isna().sum()
    if null_count > 0:
        print(f"  [WARN]  Dropped {null_count:,} rows with unparseable timestamps")
        df = df.dropna(subset=["bar_time"])

    df = df.drop(columns=["time"])

    # Add metadata columns
    df["instrument"] = instrument
    df["timeframe"]  = timeframe

    # Enforce numeric types
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(int)

    # Drop rows with any null OHLC
    before = len(df)
    df = df.dropna(subset=["open", "high", "low", "close"])
    dropped = before - len(df)
    if dropped > 0:
        print(f"  [WARN]  Dropped {dropped:,} rows with null OHLC values")

    # Final column order matching SQL insert
    df = df[["instrument", "timeframe", "bar_time",
             "open", "high", "low", "close", "volume"]]

    print(f"  Rows ready : {len(df):,}")
    print(f"  Date range : {df['bar_time'].min()} -> {df['bar_time'].max()}")

    return df


# ============================================================================
# HELPER: Insert DataFrame into dbo.raw_prices in batches
# Idempotent — skips rows that already exist
# ============================================================================

def insert_to_sql(df, conn):
    cursor   = conn.cursor()
    inserted = 0
    skipped  = 0
    total    = len(df)
    rows     = df.values.tolist()

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
            instrument, timeframe, bar_time, o, h, l, c, v = row
            try:
                cursor.execute(sql, (
                    instrument, timeframe, bar_time,   # EXISTS check params
                    instrument, timeframe, bar_time,   # INSERT params
                    o, h, l, c, v
                ))
                if cursor.rowcount == 1:
                    inserted += 1
                else:
                    skipped += 1
            except pyodbc.Error as e:
                print(f"\n  [ERROR] Row insert failed: {e}")
                skipped += 1

        conn.commit()

        # Progress indicator
        pct       = min((i + BATCH_SIZE) / total * 100, 100)
        done_rows = min(i + BATCH_SIZE, total)
        print(f"  Progress  : {pct:5.1f}%  ({done_rows:,} / {total:,})", end="\r")

    print()  # newline after progress line
    return inserted, skipped


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 60)
    print("BacktestRegime - Price ETL")
    print(f"Folder : {RAW_DATA_FOLDER}")
    print(f"Server : {SQL_SERVER}  |  DB: {SQL_DATABASE}")
    print("=" * 60)

    # Verify raw data folder
    if not os.path.exists(RAW_DATA_FOLDER):
        print(f"\n[ERROR] Folder not found:\n  {RAW_DATA_FOLDER}")
        print("Check the RAW_DATA_FOLDER path in the configuration section.")
        sys.exit(1)

    # Find CSV files
    csv_files = sorted(glob.glob(os.path.join(RAW_DATA_FOLDER, "*.csv")))
    if not csv_files:
        print(f"\n[ERROR] No CSV files found in:\n  {RAW_DATA_FOLDER}")
        sys.exit(1)

    print(f"\nFound {len(csv_files)} CSV file(s):")
    for f in csv_files:
        print(f"  {os.path.basename(f)}")

    # Connect to SQL Server
    print(f"\nConnecting to SQL Server...")
    try:
        conn = pyodbc.connect(CONNECTION_STRING, autocommit=False)
        print("  Connected.")
    except pyodbc.Error as e:
        print(f"\n[ERROR] Connection failed: {e}")
        print("\nTroubleshooting:")
        print("  1. SQL Server running on YOUR_SQL_SERVER?")
        print("  2. BacktestRegime database created? (run BacktestRegime_Schema.sql first)")
        print("  3. ODBC Driver 17 installed?")
        print("     Download: https://aka.ms/odbc17")
        sys.exit(1)

    # Process each file
    total_inserted = 0
    total_skipped  = 0
    total_errors   = 0

    for filepath in csv_files:
        print(f"\n{'-' * 50}")
        instrument, timeframe = parse_filename(filepath)

        if instrument is None:
            total_errors += 1
            continue

        print(f"  Instrument : {instrument}  |  Timeframe : {timeframe}")

        df = load_csv(filepath, instrument, timeframe)
        if df is None:
            total_errors += 1
            continue

        inserted, skipped = insert_to_sql(df, conn)
        total_inserted += inserted
        total_skipped  += skipped

        print(f"  Inserted   : {inserted:,}")
        print(f"  Skipped    : {skipped:,}  (duplicates — safe to ignore)")

    # Summary
    print(f"\n{'=' * 60}")
    print("ETL COMPLETE")
    print(f"  Total inserted       : {total_inserted:,}")
    print(f"  Total skipped (dupes): {total_skipped:,}")
    print(f"  Files with errors    : {total_errors}")
    print("=" * 60)

    # Verification query
    print("\nVerification — dbo.raw_prices row counts:\n")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            instrument,
            timeframe,
            COUNT(*)     AS bar_count,
            MIN(bar_time) AS earliest,
            MAX(bar_time) AS latest
        FROM dbo.raw_prices
        GROUP BY instrument, timeframe
        ORDER BY instrument, timeframe
    """)
    rows = cursor.fetchall()

    if rows:
        header = f"  {'Instrument':<12} {'TF':<5} {'Bars':>8}   {'Earliest':<22} {'Latest'}"
        print(header)
        print("  " + "-" * 70)
        for row in rows:
            print(f"  {row[0]:<12} {row[1]:<5} {row[2]:>8,}   {str(row[3]):<22} {str(row[4])}")
    else:
        print("  No rows found. Check errors above.")

    conn.close()
    print("\nDone.\n")


if __name__ == "__main__":
    main()
