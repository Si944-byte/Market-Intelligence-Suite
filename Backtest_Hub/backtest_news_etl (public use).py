"""
backtest_news_etl (public use).py
==================================
BacktestRegime — Step 2: News Events ETL
Database: BacktestRegime
Table:    dbo.news_events

Purpose:
    Loads high-impact macro news events into dbo.news_events.

    Source 1 — HuggingFace CSV (ff_calendar.csv)
        Covers January 2007 to April 2025.
        Filters: Impact = "High Impact Expected", Currency = USD or EUR.

    Source 2 — ForexFactory JSON feed (gap fill only)
        Fills April 2025 to today (~52 weeks max).
        Uses 3 second delay to avoid rate limiting.
        Stops gracefully if rate limited — does not crash.

Usage:
    python "backtest_news_etl (public use).py"

Dependencies:
    pip install pandas pyodbc requests python-dateutil
"""

import os
import sys
import time
import requests
import pandas as pd
import pyodbc
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateutil_parser

# ============================================================================
# CONFIGURATION — update these values for your environment
# ============================================================================
# Recommended: store credentials in a .env file and load with python-dotenv
# See README.md for setup instructions

RAW_DATA_FOLDER = r"YOUR_RAW_DATA_FOLDER"
FF_CSV_FILENAME = "ff_calendar.csv"

SQL_SERVER        = "YOUR_SQL_SERVER"
SQL_DATABASE      = "BacktestRegime"
CONNECTION_STRING = (
    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
    f"SERVER={SQL_SERVER};"
    f"DATABASE={SQL_DATABASE};"
    f"UID=YOUR_SQL_USER;PWD=YOUR_SQL_PASSWORD;"
)

# Floor for CSV events — update if you obtain a newer ff_calendar.csv covering earlier dates
BACKTEST_START   = datetime(2024, 1, 1)
# Fallback gap-fill start used only when the CSV is empty; normally derived from CSV max date
GAP_START_DATE   = datetime(2025, 4, 1, tzinfo=timezone.utc)
GAP_END_DATE     = datetime.now(timezone.utc)
FF_REQUEST_DELAY = 3.0
TARGET_CURRENCIES = {"USD", "EUR"}
HIGH_IMPACT_STRING = "high impact expected"

HIGH_IMPACT_KEYWORDS = [
    "Non-Farm Payrolls", "Nonfarm Payrolls",
    "CPI m/m", "Core CPI m/m",
    "PPI m/m", "Core PPI m/m",
    "FOMC Statement", "Federal Funds Rate", "FOMC Meeting Minutes",
    "Advance GDP", "GDP q/q",
    "Retail Sales m/m", "Core Retail Sales",
    "JOLTS Job Openings",
    "PCE Price Index", "Core PCE Price Index",
    "Unemployment Rate",
    "ISM Manufacturing PMI", "ISM Services PMI",
    "ADP Non-Farm Employment",
    "CPI Flash", "Core CPI Flash", "GDP Flash",
    "Minimum Bid Rate", "Main Refinancing Rate",
    "ECB Press Conference", "ECB Monetary Policy",
]


def is_target_event(event_name, currency, impact_str):
    if str(currency).strip().upper() not in TARGET_CURRENCIES:
        return False
    if HIGH_IMPACT_STRING not in str(impact_str).strip().lower():
        return False
    return any(kw.lower() in str(event_name).lower() for kw in HIGH_IMPACT_KEYWORDS)


def parse_to_utc_naive(ts_string):
    try:
        dt = dateutil_parser.parse(str(ts_string))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


# ============================================================================
# SOURCE 1: HuggingFace CSV
# ============================================================================

def load_csv_events():
    csv_path = os.path.join(RAW_DATA_FOLDER, FF_CSV_FILENAME)

    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"CSV not found: {csv_path}\n"
            f"Download from HuggingFace and save as '{FF_CSV_FILENAME}' in your Raw folder."
        )

    print(f"  Loading: {FF_CSV_FILENAME}")
    df = pd.read_csv(csv_path, low_memory=False)
    print(f"  Total rows in CSV   : {len(df):,}")

    df.columns = [c.strip() for c in df.columns]

    required = ["DateTime", "Currency", "Impact", "Event"]
    missing  = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns: {missing}. Found: {list(df.columns)}")

    mask = df.apply(
        lambda r: is_target_event(r["Event"], r["Currency"], r["Impact"]), axis=1
    )
    df = df[mask].copy()
    print(f"  After impact filter : {len(df):,} rows")

    df["event_time"] = df["DateTime"].apply(parse_to_utc_naive)
    df = df.dropna(subset=["event_time"])
    df = df[df["event_time"] >= BACKTEST_START]
    print(f"  After date filter   : {len(df):,} rows (>= {BACKTEST_START.date()})")

    return pd.DataFrame({
        "event_time": df["event_time"].values,
        "currency":   df["Currency"].str.strip().str.upper().values,
        "event_name": df["Event"].str.strip().values,
        "impact":     "HIGH"
    }).drop_duplicates(subset=["event_time", "event_name"]).reset_index(drop=True)


# ============================================================================
# SOURCE 2: ForexFactory gap fill (Apr 2025 -> today)
# ============================================================================

def get_monday(dt):
    return dt - timedelta(days=dt.weekday())


def fetch_ff_week(week_start):
    url     = f"https://nfs.faireconomy.media/ff_calendar_thisweek.json?week={week_start.strftime('%Y-%m-%d')}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":     "application/json",
        "Referer":    "https://www.forexfactory.com/",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 429:
            print(f"\n  [WARN] Rate limited at {week_start.date()} — stopping gap fill.")
            return None
        else:
            print(f"\n  [WARN] HTTP {r.status_code} for {week_start.date()} — skipping.")
            return []
    except Exception as e:
        print(f"\n  [WARN] Request error for {week_start.date()}: {e}")
        return []


def load_gap_events(start_date=None):
    if start_date is None:
        start_date = GAP_START_DATE
    print(f"  Gap fill: {start_date.date()} -> {GAP_END_DATE.date()}")
    events  = []
    current = get_monday(start_date)
    end     = get_monday(GAP_END_DATE) + timedelta(weeks=1)
    total   = max(int((end - current).days / 7), 1)
    done    = 0

    while current <= end:
        data = fetch_ff_week(current)
        if data is None:
            break

        for item in data:
            name     = str(item.get("title",   "")).strip()
            currency = str(item.get("country", "")).strip().upper()
            impact   = str(item.get("impact",  "")).strip().lower()
            date_str = str(item.get("date",    "")).strip()

            if currency not in TARGET_CURRENCIES:
                continue
            if impact != "high":
                continue
            if not any(kw.lower() in name.lower() for kw in HIGH_IMPACT_KEYWORDS):
                continue

            dt = parse_to_utc_naive(date_str)
            if dt is None:
                continue

            events.append({
                "event_time": dt,
                "currency":   currency,
                "event_name": name,
                "impact":     "HIGH"
            })

        done += 1
        time.sleep(FF_REQUEST_DELAY)
        print(f"  Progress: {min(done/total*100,100):.0f}%  ({done}/{total} weeks)", end="\r")
        current += timedelta(weeks=1)

    print()

    if not events:
        print("  No gap events retrieved — FF unavailable or no matches.")
        return pd.DataFrame(columns=["event_time", "currency", "event_name", "impact"])

    df = pd.DataFrame(events).drop_duplicates(subset=["event_time", "event_name"])
    print(f"  Gap events retrieved: {len(df):,}")
    return df


# ============================================================================
# SQL INSERT
# ============================================================================

def insert_to_sql(df, conn):
    cursor   = conn.cursor()
    inserted = 0
    skipped  = 0

    sql = """
        IF NOT EXISTS (
            SELECT 1 FROM dbo.news_events
            WHERE event_time = ? AND event_name = ?
        )
        INSERT INTO dbo.news_events (event_time, currency, event_name, impact)
        VALUES (?, ?, ?, ?)
    """

    for _, row in df.iterrows():
        try:
            cursor.execute(sql, (
                row["event_time"], row["event_name"],
                row["event_time"], row["currency"],
                row["event_name"], row["impact"]
            ))
            inserted += 1 if cursor.rowcount > 0 else 0
            skipped  += 1 if cursor.rowcount == 0 else 0
        except pyodbc.Error as e:
            print(f"\n  [ERROR] Insert failed: {e}")
            skipped += 1

    conn.commit()
    return inserted, skipped


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 60)
    print("BacktestRegime — News Events ETL")
    print(f"Server : {SQL_SERVER}  |  DB: {SQL_DATABASE}")
    print("=" * 60)

    # Step 1 — CSV
    print("\nStep 1 — Loading HuggingFace CSV...")
    try:
        df_csv = load_csv_events()
        print(f"  CSV events ready    : {len(df_csv):,}")
    except (FileNotFoundError, ValueError) as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)

    # Step 2 — Gap fill (start from CSV max date so we never re-fetch known events)
    if not df_csv.empty:
        csv_max = df_csv["event_time"].max()
        gap_start = datetime(csv_max.year, csv_max.month, csv_max.day, tzinfo=timezone.utc)
    else:
        gap_start = GAP_START_DATE
    print(f"\nStep 2 — FF gap fill ({gap_start.date()} -> today)...")
    df_gap = load_gap_events(start_date=gap_start)

    # Step 3 — Combine
    df_all = (
        pd.concat([df_csv, df_gap], ignore_index=True)
        .drop_duplicates(subset=["event_time", "event_name"])
        .sort_values("event_time")
        .reset_index(drop=True)
    )

    print(f"\n  Combined total      : {len(df_all):,} events")
    print(f"  Date range          : {df_all['event_time'].min()} -> {df_all['event_time'].max()}")

    print("\n  Event breakdown:")
    summary = (
        df_all.groupby(["currency", "event_name"])
        .size().reset_index(name="count")
        .sort_values(["currency", "event_name"])
    )
    for _, row in summary.iterrows():
        print(f"    {row['currency']:<5} {row['event_name']:<45} {row['count']:>3}x")

    # Step 4 — SQL
    print("\nStep 3 — Connecting to SQL Server...")
    try:
        conn = pyodbc.connect(CONNECTION_STRING, autocommit=False)
        print("  Connected.")
    except pyodbc.Error as e:
        print(f"\n[ERROR] Connection failed: {e}")
        sys.exit(1)

    print("\nStep 4 — Inserting into dbo.news_events...")
    inserted, skipped = insert_to_sql(df_all, conn)
    print(f"  Inserted : {inserted:,}")
    print(f"  Skipped  : {skipped:,}  (duplicates)")

    # Verify
    print("\nVerification — dbo.news_events:\n")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT currency, event_name, COUNT(*) AS ct,
               MIN(event_time) AS earliest, MAX(event_time) AS latest
        FROM dbo.news_events
        GROUP BY currency, event_name
        ORDER BY currency, event_name
    """)
    rows = cursor.fetchall()
    if rows:
        print(f"  {'CCY':<5} {'Event':<45} {'Ct':>4}  {'Earliest':<12} {'Latest'}")
        print("  " + "-" * 82)
        for r in rows:
            print(f"  {r[0]:<5} {r[1]:<45} {r[2]:>4}  {str(r[3])[:10]:<12} {str(r[4])[:10]}")
        print(f"\n  Total: {sum(r[2] for r in rows):,} events in database")
    else:
        print("  No rows found — check errors above.")

    conn.close()
    print("\nDone.\n")


if __name__ == "__main__":
    main()
