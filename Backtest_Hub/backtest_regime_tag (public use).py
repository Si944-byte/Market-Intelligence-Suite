"""
backtest_regime_tag (public use).py
=====================================
BacktestRegime — Step 4: Regime Tagger
Database: BacktestRegime
Table:    dbo.regime_tags

Purpose:
    For every signal in dbo.signals_mart, looks up the active regime
    context from your three existing intelligence databases at the time
    of signal entry. Writes results to dbo.regime_tags.

Exact table/column mapping (verified from ETL source code):

    MacroRegime.dbo.macro_monthly
        date column : date
        regime col  : regime_label  (Goldilocks / Inflation / Stagflation / Recession)

    SentimentRegime.dbo.sentiment_daily
        date column : date
        regime col  : sentiment_label (Extreme Fear / Fear / Neutral / Greed / Extreme Greed)
        score col   : composite_zscore
        vix col     : vix_zscore

    COTRegime.dbo.cot_weekly
        date column : report_date
        instrument  : symbol (ES, NQ, YM, GC, SI, CL, NG, EC, TY, US, C, S)
        regime col  : positioning_label (Extreme Long / Long / Neutral / Short / Extreme Short)
        zscore col  : primary_zscore (COALESCE of noncomm_zscore / mm_zscore)

Confluence Score (0-3, one point per dashboard):
    +1 if macro_regime aligns with signal direction
    +1 if sentiment_label aligns with signal direction
    +1 if cot positioning_label aligns with signal direction

    Label: 0-1 = Low | 2 = Medium | 3 = High

Prerequisites:
    This script connects to MacroRegime, SentimentRegime, and COTRegime
    databases. These must be populated by their respective ETL scripts
    (Macro_Inflation_Watch, Sentiment_Hub, COT_Hub) before running this step.

Usage:
    python "backtest_regime_tag (public use).py"
    python "backtest_regime_tag (public use).py" --clear

Dependencies:
    pip install pandas pyodbc
"""

import sys
import argparse
import pyodbc
from datetime import datetime

# ============================================================================
# CONFIGURATION — update these values for your environment
# ============================================================================
# Recommended: store credentials in a .env file and load with python-dotenv
# See README.md for setup instructions

SQL_SERVER   = "YOUR_SQL_SERVER"
SQL_DATABASE = "BacktestRegime"
MACRO_DB     = "MacroRegime"
SENTIMENT_DB = "SentimentRegime"
COT_DB       = "COTRegime"

BASE_CONN = (
    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
    f"SERVER={SQL_SERVER};"
    f"UID=YOUR_SQL_USER;PWD=YOUR_SQL_PASSWORD;"
)

def get_connection(database):
    try:
        conn = pyodbc.connect(BASE_CONN + f"DATABASE={database};", autocommit=False)
        return conn
    except pyodbc.Error as e:
        print(f"  [WARN] Could not connect to {database}: {e}")
        return None


# ============================================================================
# COT SYMBOL MAPPING
# Maps our instrument codes to the symbol column in COTRegime.dbo.cot_weekly
# Verified from COT ETL instrument master
# ============================================================================

COT_SYMBOL_MAP = {
    "MES1": "ES",
    "MNQ1": "NQ",
    "MYM1": "YM",
    "MGC1": "GC",
    "MCL1": "CL",
    "SIL1": "SI",
    "CL1":  "CL",
    "NG1":  "NG",
    "6E1":  "EC",
    "ZN1":  "TY",
    "ZB1":  "US",
    "ZC1":  "C",
    "ZS1":  "S",
}


# ============================================================================
# REGIME LOOKUP FUNCTIONS
# Each uses TOP 1 ... WHERE date <= entry_time ORDER BY date DESC
# This is the same OUTER APPLY pattern your vw_ConfluenceSignals uses
# ============================================================================

def get_macro_regime(conn, entry_time):
    """
    MacroRegime.dbo.macro_monthly
    Returns most recent regime_label on or before entry_time.
    """
    result = {"macro_regime": None, "gdp_trend": None, "inflation_regime": None}
    if conn is None:
        return result
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT TOP 1
                regime_label,
                gdp_smoothed,
                cpi_smoothed
            FROM MacroRegime.dbo.macro_monthly
            WHERE date <= ?
            ORDER BY date DESC
        """, entry_time)
        row = cursor.fetchone()
        if row:
            result["macro_regime"]     = str(row[0]) if row[0] else None
            result["gdp_trend"]        = float(row[1]) if row[1] is not None else None
            result["inflation_regime"] = float(row[2]) if row[2] is not None else None
    except pyodbc.Error as e:
        print(f"  [WARN] MacroRegime query failed: {e}")
    return result


def get_sentiment_regime(conn, entry_time):
    """
    SentimentRegime.dbo.sentiment_daily
    Returns most recent sentiment_label on or before entry_time.
    """
    result = {"sentiment_regime": None, "fear_greed_score": None, "vix_zscore": None}
    if conn is None:
        return result
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT TOP 1
                sentiment_label,
                composite_zscore,
                vix_zscore
            FROM SentimentRegime.dbo.sentiment_daily
            WHERE date <= ?
            ORDER BY date DESC
        """, entry_time)
        row = cursor.fetchone()
        if row:
            result["sentiment_regime"] = str(row[0]) if row[0] else None
            result["fear_greed_score"] = float(row[1]) if row[1] is not None else None
            result["vix_zscore"]       = float(row[2]) if row[2] is not None else None
    except pyodbc.Error as e:
        print(f"  [WARN] SentimentRegime query failed: {e}")
    return result


def get_cot_regime(conn, entry_time, instrument):
    """
    COTRegime.dbo.cot_weekly
    Returns most recent positioning_label and primary_zscore
    for the given instrument on or before entry_time.
    """
    result = {"cot_bias": None, "cot_zscore": None}
    if conn is None:
        return result

    cot_symbol = COT_SYMBOL_MAP.get(instrument)
    if not cot_symbol:
        print(f"  [WARN] No COT symbol mapping for instrument: {instrument}")
        return result

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT TOP 1
                positioning_label,
                COALESCE(noncomm_zscore, mm_zscore) AS primary_zscore
            FROM COTRegime.dbo.cot_weekly
            WHERE symbol = ?
              AND report_date <= ?
            ORDER BY report_date DESC
        """, (cot_symbol, entry_time))
        row = cursor.fetchone()
        if row:
            result["cot_bias"]   = str(row[0]) if row[0] else None
            result["cot_zscore"] = float(row[1]) if row[1] is not None else None
    except pyodbc.Error as e:
        print(f"  [WARN] COTRegime query failed for {cot_symbol}: {e}")
    return result


# ============================================================================
# CONFLUENCE SCORING
# ============================================================================

def calc_confluence(direction, macro_regime, sentiment_label, cot_label):
    """
    Score 0-3: one point per dashboard that aligns with signal direction.

    LONG aligns with:
        Macro     : Goldilocks, Recovery (growth-oriented regimes)
        Sentiment : Greed, Extreme Greed (risk-on)
        COT       : Long, Extreme Long (speculative crowd is long)

    SHORT aligns with:
        Macro     : Recession, Stagflation (contraction-oriented regimes)
        Sentiment : Fear, Extreme Fear (risk-off)
        COT       : Short, Extreme Short (speculative crowd is short)
    """
    score   = 0
    is_long = (direction == "LONG")

    # Macro alignment
    if macro_regime:
        m = macro_regime.lower()
        if is_long  and any(x in m for x in ["goldilocks", "recovery", "expansion"]):
            score += 1
        elif not is_long and any(x in m for x in ["recession", "stagflation", "contraction"]):
            score += 1

    # Sentiment alignment
    if sentiment_label:
        s = sentiment_label.lower()
        if is_long  and any(x in s for x in ["greed"]):
            score += 1
        elif not is_long and any(x in s for x in ["fear"]):
            score += 1

    # COT alignment
    if cot_label:
        c = cot_label.lower()
        if is_long  and any(x in c for x in ["long"]):
            score += 1
        elif not is_long and any(x in c for x in ["short"]):
            score += 1

    return score


def get_confluence_label(score):
    if score >= 3:
        return "High"
    elif score == 2:
        return "Medium"
    else:
        return "Low"


# ============================================================================
# MAIN TAGGING LOGIC
# ============================================================================

def tag_all_signals(conn_bt, macro_conn, sent_conn, cot_conn):
    cursor_bt = conn_bt.cursor()

    # Get all untagged signals
    cursor_bt.execute("""
        SELECT s.signal_id, s.instrument, s.direction,
               CAST(s.entry_time AS DATETIME) AS entry_time
        FROM dbo.signals_mart s
        LEFT JOIN dbo.regime_tags r ON s.signal_id = r.signal_id
        WHERE r.tag_id IS NULL
        ORDER BY s.entry_time ASC
    """)
    signals = cursor_bt.fetchall()

    if not signals:
        print("  No untagged signals found.")
        return 0

    print(f"  Tagging {len(signals)} signal(s)...\n")

    insert_sql = """
        INSERT INTO dbo.regime_tags (
            signal_id,
            liquidity_regime, liquidity_score,
            macro_regime, gdp_trend, inflation_regime,
            sentiment_regime, fear_greed_score, vix_zscore,
            cot_bias, cot_zscore,
            confluence_score, confluence_label,
            regime_aligned
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    tagged = 0

    for signal_id, instrument, direction, entry_time in signals:

        macro = get_macro_regime(macro_conn, entry_time)
        sent  = get_sentiment_regime(sent_conn, entry_time)
        cot   = get_cot_regime(cot_conn, entry_time, instrument)

        score   = calc_confluence(
            direction,
            macro["macro_regime"],
            sent["sentiment_regime"],
            cot["cot_bias"]
        )
        label   = get_confluence_label(score)
        aligned = 1 if score >= 2 else 0

        print(f"  Signal {signal_id:>4} | {instrument:<5} {direction:<5} | "
              f"Macro: {str(macro['macro_regime']):<15} "
              f"Sent: {str(sent['sentiment_regime']):<15} "
              f"COT: {str(cot['cot_bias']):<14} "
              f"Score: {score} ({label})")

        try:
            cursor_bt.execute(insert_sql, (
                signal_id,
                None, None,                         # liquidity (Phase 2)
                macro["macro_regime"],
                macro["gdp_trend"],
                macro["inflation_regime"],
                sent["sentiment_regime"],
                sent["fear_greed_score"],
                sent["vix_zscore"],
                cot["cot_bias"],
                cot["cot_zscore"],
                score, label, aligned
            ))
            conn_bt.commit()
            tagged += 1
        except pyodbc.Error as e:
            print(f"  [ERROR] Insert failed for signal {signal_id}: {e}")

    return tagged


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="BacktestRegime Regime Tagger")
    parser.add_argument("--clear", action="store_true",
                        help="Clear existing regime tags before running")
    args = parser.parse_args()

    print("=" * 60)
    print("BacktestRegime — Regime Tagger")
    print(f"Server : {SQL_SERVER}")
    print("=" * 60)

    # Connections
    print("\nConnecting to databases...")
    conn_bt  = get_connection(SQL_DATABASE)
    if conn_bt is None:
        print("[ERROR] Cannot connect to BacktestRegime. Exiting.")
        sys.exit(1)
    print(f"  BacktestRegime  : Connected")

    macro_conn = get_connection(MACRO_DB)
    print(f"  MacroRegime     : {'Connected' if macro_conn else 'UNAVAILABLE — tagging as NULL'}")

    sent_conn = get_connection(SENTIMENT_DB)
    print(f"  SentimentRegime : {'Connected' if sent_conn else 'UNAVAILABLE — tagging as NULL'}")

    cot_conn = get_connection(COT_DB)
    print(f"  COTRegime       : {'Connected' if cot_conn else 'UNAVAILABLE — tagging as NULL'}")

    # Optional clear
    if args.clear:
        print("\nClearing existing tags...")
        cursor = conn_bt.cursor()
        cursor.execute("DELETE FROM dbo.regime_tags")
        conn_bt.commit()
        print("  Cleared.")

    # Tag
    print("\nTagging signals...")
    tagged = tag_all_signals(conn_bt, macro_conn, sent_conn, cot_conn)
    print(f"\n  Total tagged: {tagged}")

    # Verification
    print("\nVerification — dbo.regime_tags:\n")
    cursor = conn_bt.cursor()
    cursor.execute("""
        SELECT
            s.instrument,
            s.direction,
            CAST(s.entry_time AS DATE)  AS entry_date,
            r.macro_regime,
            r.sentiment_regime,
            r.cot_bias,
            r.confluence_score,
            r.confluence_label,
            r.regime_aligned
        FROM dbo.regime_tags r
        JOIN dbo.signals_mart s ON r.signal_id = s.signal_id
        ORDER BY s.entry_time ASC
    """)
    rows = cursor.fetchall()

    if rows:
        print(f"  {'Inst':<6} {'Dir':<6} {'Date':<12} {'Macro':<16} "
              f"{'Sentiment':<16} {'COT':<16} {'Score':<6} {'Label':<8} {'Algnd'}")
        print("  " + "-" * 100)
        for r in rows:
            print(
                f"  {str(r[0]):<6} {str(r[1]):<6} {str(r[2]):<12} "
                f"{str(r[3]):<16} {str(r[4]):<16} {str(r[5]):<16} "
                f"{str(r[6]):<6} {str(r[7]):<8} {str(r[8])}"
            )
    else:
        print("  No tags found.")

    for c in [conn_bt, macro_conn, sent_conn, cot_conn]:
        if c:
            c.close()

    print("\nDone.\n")


if __name__ == "__main__":
    main()
