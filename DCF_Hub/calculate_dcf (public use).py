"""
calculate_dcf.py
────────────────
Reads the latest fundamentals from SQLite, runs the full DCF
calculation for all S&P 500 stocks, assigns Quality Tiers and
Signals, then writes to SQL Server (DCFRegime) AND exports
Stock_Data_Current.csv as a backup.

DCF Assumptions:
  - Sector-specific growth rates and discount rates (Base Case)
  - Conservative: 5% growth, 10% discount (all sectors)
  - Aggressive:   15% growth, 7% discount (all sectors)
  - Terminal growth: 2.5% (all scenarios, all sectors)
  - Projection period: 5 years

Usage:
    python calculate_dcf.py

Requirements:
    pip install pandas pyodbc
"""

import sqlite3
import pandas as pd
import numpy as np
import pyodbc
import os
from datetime import datetime
from dotenv import load_dotenv
from etl_utils import managed_conn

# ── CONFIG ────────────────────────────────────────────────────────────────────
load_dotenv()

DB_PATH     = os.environ.get("DCF_DB_PATH",     r".\sp500_prices.db")
OUTPUT_PATH = os.environ.get("DCF_OUTPUT_PATH", r".\Stock_Data_Current.csv")

SQL_SERVER   = os.environ.get("SQL_SERVER",   "YOUR_SQL_SERVER")
SQL_DATABASE = os.environ.get("SQL_DATABASE", "DCFRegime")
SQL_USER     = os.environ.get("SQL_USER",     "dcf_user")
SQL_PASSWORD = os.environ.get("SQL_PASSWORD", "YOUR_SQL_PASSWORD")

TERMINAL_GROWTH = 0.025
DCF_YEARS       = 5

# ── SECTOR-SPECIFIC BASE CASE ASSUMPTIONS ────────────────────────────────────
# Format: 'Sector': (growth_rate, discount_rate)
SECTOR_ASSUMPTIONS = {
    'Information Technology':  (0.12, 0.09),
    'Consumer Discretionary':  (0.09, 0.09),
    'Communication Services':  (0.10, 0.09),
    'Health Care':             (0.09, 0.08),
    'Industrials':             (0.08, 0.08),
    'Materials':               (0.07, 0.08),
    'Energy':                  (0.07, 0.08),
    'Consumer Staples':        (0.06, 0.07),
    'Utilities':               (0.05, 0.07),
    'Real Estate':             (0.06, 0.07),
    'Financials':              (0.08, 0.08),
}

# Default for any sector not in the map
DEFAULT_ASSUMPTIONS = (0.08, 0.08)

# ── SCENARIO ASSUMPTIONS (fixed, all sectors) ─────────────────────────────────
SCENARIOS = {
    'Conservative': (0.05, 0.10),
    'Aggressive':   (0.15, 0.07),
}

# ── QUALITY TIER LOGIC ────────────────────────────────────────────────────────
def assign_quality_tier(profit_margin, debt_to_equity, sector):
    """
    Assign quality tier based on profitability and leverage.
    Financials and Utilities get adjusted thresholds.
    """
    if profit_margin is None or np.isnan(profit_margin):
        return 'Low'

    # Sector adjustments
    if sector == 'Financials':
        # Financials have lower margins but that's structural
        if profit_margin > 0.15 and (debt_to_equity is None or debt_to_equity < 2.0):
            return 'High'
        elif profit_margin > 0.05:
            return 'Medium'
        else:
            return 'Low'
    elif sector == 'Utilities':
        # Utilities carry structural debt — use looser D/E threshold
        if profit_margin > 0.10 and (debt_to_equity is None or debt_to_equity < 1.5):
            return 'High'
        elif profit_margin > 0.05:
            return 'Medium'
        else:
            return 'Low'
    elif sector == 'Real Estate':
        if profit_margin > 0.10:
            return 'High'
        elif profit_margin > 0.03:
            return 'Medium'
        else:
            return 'Low'
    else:
        # Standard logic
        de = debt_to_equity if debt_to_equity is not None else 0
        if profit_margin > 0.15 and de < 0.5:
            return 'High'
        elif profit_margin > 0.05 and de < 1.0:
            return 'Medium'
        else:
            return 'Low'


# ── DCF CALCULATION ───────────────────────────────────────────────────────────
def calculate_dcf(fcf, growth, discount, terminal=TERMINAL_GROWTH, years=DCF_YEARS):
    """
    Calculate total DCF value (not per share).
    Returns None if FCF is missing or zero.
    """
    if fcf is None or fcf == 0 or np.isnan(fcf):
        return None
    if discount <= terminal:
        return None

    # Sum of discounted cash flows for projection period
    pv_sum = sum(
        (fcf * (1 + growth) ** t) / (1 + discount) ** t
        for t in range(1, years + 1)
    )

    # Terminal value
    fcf_terminal = fcf * (1 + growth) ** years
    terminal_value = (fcf_terminal * (1 + terminal)) / (discount - terminal)
    pv_terminal = terminal_value / (1 + discount) ** years

    return pv_sum + pv_terminal


def intrinsic_per_share(total_value, market_cap, current_price):
    """Convert total DCF value to per-share intrinsic value."""
    if total_value is None or current_price is None or current_price == 0:
        return None
    if market_cap and market_cap > 0:
        shares = market_cap / current_price
    else:
        return None
    if shares == 0:
        return None
    return total_value / shares


def valuation_gap(intrinsic, current_price):
    """Calculate valuation gap percentage."""
    if intrinsic is None or current_price is None or current_price == 0:
        return None
    return (intrinsic - current_price) / current_price


def assign_signal(gap):
    """Assign BUY/HOLD/SELL based on valuation gap."""
    if gap is None or np.isnan(gap):
        return 'INSUFFICIENT DATA'
    elif gap > 0.10:
        return 'BUY'
    elif gap < -0.10:
        return 'SELL'
    else:
        return 'HOLD'


# ── LOAD DATA ─────────────────────────────────────────────────────────────────
def load_latest_fundamentals():
    """Load the most recent fundamentals snapshot from SQLite."""
    conn = sqlite3.connect(DB_PATH)

    latest_date = conn.execute(
        "SELECT MAX(fetch_date) FROM fundamentals"
    ).fetchone()[0]

    if not latest_date:
        conn.close()
        raise ValueError("No fundamentals data found. Run fetch_fundamentals.py first.")

    print(f"  Loading fundamentals — fetch date: {latest_date}")

    df = pd.read_sql_query("""
        SELECT
            ticker, company, sector,
            current_price, market_cap, revenue,
            fcf, operating_cash_flow, capital_expenditure,
            net_income, total_debt, debt_to_equity,
            profit_margin, operating_margin,
            week52_high, week52_low, dcf_method,
            fetch_date
        FROM fundamentals
        WHERE fetch_date = ?
        ORDER BY ticker
    """, conn, params=(latest_date,))

    conn.close()
    print(f"  {len(df)} stocks loaded.")
    return df, latest_date


# ── MAIN CALCULATION ──────────────────────────────────────────────────────────
def run_calculations(df):
    """Run all DCF calculations and assign tiers/signals using numpy broadcasting."""
    print(f"\n  Running DCF calculations for {len(df)} stocks...")

    sector_arr   = df['sector'].fillna('Unknown').values
    growth_arr   = np.array([SECTOR_ASSUMPTIONS.get(s, DEFAULT_ASSUMPTIONS)[0] for s in sector_arr])
    discount_arr = np.array([SECTOR_ASSUMPTIONS.get(s, DEFAULT_ASSUMPTIONS)[1] for s in sector_arr])

    fcf_arr    = pd.to_numeric(df['fcf'],           errors='coerce').values.astype(float)
    price_arr  = pd.to_numeric(df['current_price'], errors='coerce').values.astype(float)
    mktcap_arr = pd.to_numeric(df['market_cap'],    errors='coerce').values.astype(float)

    def _dcf_vec(fcf, growth, discount):
        growth   = np.broadcast_to(np.asarray(growth,   dtype=float), fcf.shape).copy()
        discount = np.broadcast_to(np.asarray(discount, dtype=float), fcf.shape).copy()
        t = np.arange(1, DCF_YEARS + 1)
        pv = fcf[:, None] * (1 + growth[:, None]) ** t / (1 + discount[:, None]) ** t
        pv_sum = pv.sum(axis=1)
        fcf_terminal   = fcf * (1 + growth) ** DCF_YEARS
        terminal_value = (fcf_terminal * (1 + TERMINAL_GROWTH)) / (discount - TERMINAL_GROWTH)
        pv_terminal    = terminal_value / (1 + discount) ** DCF_YEARS
        result = pv_sum + pv_terminal
        invalid = np.isnan(fcf) | (fcf == 0) | (discount <= TERMINAL_GROWTH)
        result[invalid] = np.nan
        return result

    base_total_arr = _dcf_vec(fcf_arr, growth_arr,       discount_arr)
    cons_total_arr = _dcf_vec(fcf_arr, SCENARIOS['Conservative'][0], SCENARIOS['Conservative'][1])
    aggr_total_arr = _dcf_vec(fcf_arr, SCENARIOS['Aggressive'][0],   SCENARIOS['Aggressive'][1])

    valid_price  = (price_arr > 0) & ~np.isnan(price_arr)
    valid_mktcap = (mktcap_arr > 0) & ~np.isnan(mktcap_arr)
    shares = np.where(valid_price & valid_mktcap, mktcap_arr / price_arr, np.nan)

    def _iv(total): return np.where(~np.isnan(total) & ~np.isnan(shares), total / shares, np.nan)
    def _gap(iv):   return np.where(~np.isnan(iv) & valid_price, (iv - price_arr) / price_arr, np.nan)

    base_iv_arr  = _iv(base_total_arr)
    cons_iv_arr  = _iv(cons_total_arr)
    aggr_iv_arr  = _iv(aggr_total_arr)
    base_gap_arr = _gap(base_iv_arr)
    cons_gap_arr = _gap(cons_iv_arr)
    aggr_gap_arr = _gap(aggr_iv_arr)

    signal_arr     = np.where(np.isnan(base_gap_arr), 'INSUFFICIENT DATA',
                     np.where(base_gap_arr > 0.10, 'BUY',
                     np.where(base_gap_arr < -0.10, 'SELL', 'HOLD')))
    fcf_yield_arr  = np.where(valid_mktcap & ~np.isnan(fcf_arr), fcf_arr / mktcap_arr, np.nan)
    gap_dollars_arr = np.where(~np.isnan(base_iv_arr) & valid_price & valid_mktcap,
                               (base_iv_arr - price_arr) * (mktcap_arr / price_arr), np.nan)

    quality_tiers = df.apply(
        lambda r: assign_quality_tier(r['profit_margin'], r['debt_to_equity'],
                                      r['sector'] if r['sector'] else 'Unknown'),
        axis=1,
    ).values

    def _r(arr, dp):
        return [None if np.isnan(float(v)) else round(float(v), dp) for v in arr]

    return pd.DataFrame({
        'Ticker':                    df['ticker'].values,
        'Company':                   df['company'].values,
        'Sector':                    sector_arr,
        'Current_Price':             _r(price_arr, 4),
        'Intrinsic_Value_Per_Share': _r(base_iv_arr, 4),
        'Intrinsic_Value_Total':     _r(base_total_arr, 2),
        'Valuation_Gap_Pct':         _r(base_gap_arr, 6),
        'Valuation_Gap_Dollars':     _r(gap_dollars_arr, 2),
        'Market_Cap':                df['market_cap'].values,
        'FCF':                       df['fcf'].values,
        'FCF_Yield_Pct':             _r(fcf_yield_arr, 6),
        'Revenue':                   df['revenue'].values,
        'Total_Debt':                df['total_debt'].values,
        'Debt_to_Equity':            df['debt_to_equity'].values,
        'Operating_Cash_Flow':       df['operating_cash_flow'].values,
        'Capital_Expenditure':       df['capital_expenditure'].values,
        'Profit_Margin':             df['profit_margin'].values,
        'Operating_Margin':          df['operating_margin'].values,
        'Week52_Low':                df['week52_low'].values,
        'Week52_High':               df['week52_high'].values,
        'Quality_Tier':              quality_tiers,
        'Signal':                    signal_arr,
        'DCF_Method':                df['dcf_method'].values,
        'Sector_Growth_Rate':        growth_arr,
        'Sector_Discount_Rate':      discount_arr,
        'Conservative_IV':           _r(cons_iv_arr, 4),
        'Conservative_Gap':          _r(cons_gap_arr, 6),
        'Aggressive_IV':             _r(aggr_iv_arr, 4),
        'Aggressive_Gap':            _r(aggr_gap_arr, 6),
        'Data_Date':                 df['fetch_date'].values,
    })


# ── SQL SERVER CONNECTION ─────────────────────────────────────────────────────
def get_sql_connection():
    """Connect to SQL Server DCFRegime database."""
    conn_str = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={SQL_SERVER};"
        f"DATABASE={SQL_DATABASE};"
        f"UID={SQL_USER};"
        f"PWD={SQL_PASSWORD};"
    )
    return pyodbc.connect(conn_str)


# ── WRITE TO SQL SERVER ───────────────────────────────────────────────────────
def write_to_sql(df):
    """
    Write DCF results to SQL Server dcf_results table.
    Skips rows that already exist for the same Data_Date + Ticker
    (handled by the UNIQUE constraint in the table).
    Inserts in batches of 100 for performance.
    """
    print(f"\n  Writing {len(df)} rows to SQL Server DCFRegime...")

    insert_sql = """
        INSERT INTO dbo.dcf_results (
            Data_Date, Ticker, Company, Sector,
            Current_Price, Intrinsic_Value_Per_Share, Intrinsic_Value_Total,
            Valuation_Gap_Pct, Valuation_Gap_Dollars,
            Market_Cap, FCF, FCF_Yield_Pct, Revenue, Total_Debt,
            Debt_to_Equity, Operating_Cash_Flow, Capital_Expenditure,
            Profit_Margin, Operating_Margin, Week52_Low, Week52_High,
            Quality_Tier, Signal, DCF_Method,
            Sector_Growth_Rate, Sector_Discount_Rate,
            Conservative_IV, Conservative_Gap,
            Aggressive_IV, Aggressive_Gap
        )
        SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        WHERE NOT EXISTS (
            SELECT 1 FROM dbo.dcf_results
            WHERE Data_Date = ? AND Ticker = ?
        )
    """

    inserted = 0
    skipped  = 0
    errors   = []

    with managed_conn(SQL_SERVER, SQL_DATABASE, SQL_USER, SQL_PASSWORD) as conn:
        cursor = conn.cursor()

        for _, row in df.iterrows():
            try:
                def v(col):
                    val = row.get(col)
                    if val is None:
                        return None
                    try:
                        if np.isnan(val):
                            return None
                    except:
                        pass
                    return val

                params = (
                    v('Data_Date'), v('Ticker'), v('Company'), v('Sector'),
                    v('Current_Price'), v('Intrinsic_Value_Per_Share'), v('Intrinsic_Value_Total'),
                    v('Valuation_Gap_Pct'), v('Valuation_Gap_Dollars'),
                    v('Market_Cap'), v('FCF'), v('FCF_Yield_Pct'), v('Revenue'), v('Total_Debt'),
                    v('Debt_to_Equity'), v('Operating_Cash_Flow'), v('Capital_Expenditure'),
                    v('Profit_Margin'), v('Operating_Margin'), v('Week52_Low'), v('Week52_High'),
                    v('Quality_Tier'), v('Signal'), v('DCF_Method'),
                    v('Sector_Growth_Rate'), v('Sector_Discount_Rate'),
                    v('Conservative_IV'), v('Conservative_Gap'),
                    v('Aggressive_IV'), v('Aggressive_Gap'),
                    # WHERE NOT EXISTS params
                    v('Data_Date'), v('Ticker')
                )

                cursor.execute(insert_sql, params)

                if cursor.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1

            except Exception as e:
                errors.append(f"{row.get('Ticker', '?')}: {e}")

    print(f"  Inserted: {inserted} new rows")
    print(f"  Skipped:  {skipped} already existed for this date")

    if errors:
        print(f"  Errors:   {len(errors)}")
        for e in errors[:10]:
            print(f"    {e}")

    return inserted, skipped


# ── SUMMARY ───────────────────────────────────────────────────────────────────
def print_summary(df):
    total    = len(df)
    buy      = (df['Signal'] == 'BUY').sum()
    hold     = (df['Signal'] == 'HOLD').sum()
    sell     = (df['Signal'] == 'SELL').sum()
    no_data  = (df['Signal'] == 'INSUFFICIENT DATA').sum()
    robust   = ((df['Valuation_Gap_Pct'] > 0.10) & (df['Conservative_Gap'] > 0.10)).sum()
    downside = ((df['Valuation_Gap_Pct'] > 0.10) & (df['Conservative_Gap'] < 0)).sum()

    print(f"\n  {'-'*40}")
    print(f"  PORTFOLIO SUMMARY ({total} stocks)")
    print(f"  {'-'*40}")
    print(f"  BUY signals:          {buy}")
    print(f"  HOLD signals:         {hold}")
    print(f"  SELL signals:         {sell}")
    print(f"  Insufficient data:    {no_data}")
    print(f"  Robust BUY (all scenarios): {robust}")
    print(f"  Downside Risk:        {downside}")
    print(f"\n  Avg Valuation Gap:    {df['Valuation_Gap_Pct'].mean()*100:.1f}%")
    print(f"\n  By Sector:")
    sector_summary = df.groupby('Sector')['Valuation_Gap_Pct'].mean().sort_values(ascending=False)
    for sector, gap in sector_summary.items():
        sign = "+" if gap > 0 else ""
        print(f"    {sector:<35} {sign}{gap*100:.1f}%")


# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  S&P 500 DCF Calculator & SQL Server Writer")
    print("=" * 60)

    if not os.path.exists(DB_PATH):
        print(f"\n  ERROR: Database not found.")
        print(f"  Run fetch_fundamentals.py first.")
        return

    df_raw, latest_date = load_latest_fundamentals()
    df_out = run_calculations(df_raw)

    # Sort by Valuation Gap descending (best opportunities first)
    df_out = df_out.sort_values('Valuation_Gap_Pct', ascending=False, na_position='last')

    # ── Write to SQL Server ───────────────────────────────────────────────────
    try:
        inserted, skipped = write_to_sql(df_out)
    except Exception as e:
        print(f"\n  SQL Server write failed: {e}")
        print(f"  Falling back to CSV export only.")

    # ── CSV backup export (unchanged) ────────────────────────────────────────
    df_out.to_csv(OUTPUT_PATH, index=False)

    print_summary(df_out)

    print(f"\n{'='*60}")
    print(f"  COMPLETE")
    print(f"  Data as of:    {latest_date}")
    print(f"  Stocks:        {len(df_out)}")
    print(f"  SQL Server:    DCFRegime.dbo.dcf_results")
    print(f"  CSV backup:    {OUTPUT_PATH}")
    print(f"{'='*60}")
    print(f"\n  Open Power BI and hit Refresh.")


if __name__ == "__main__":
    main()
