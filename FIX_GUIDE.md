# Fix Guide — Market Intelligence Suite
*Generated from audit session: 2026-06-22*

This document is a consolidated, prioritized reference for fixing the technical debt identified across all six ETL pipelines. Read alongside `TECHNICAL_DEBT.md` for the full debt inventory and `docs/architecture.html` for the system diagram.

---

## Quick Reference

| Hub | Script | Database |
|---|---|---|
| COT Positioning | `COT_Hub/cot_etl - public use.py` | COTRegime |
| DCF Valuation (Stage 1) | `DCF_Hub/fetch_fundamentals_rapidapi (public use).py` | SQLite → sp500_prices.db |
| DCF Valuation (Stage 2) | `DCF_Hub/calculate_dcf (public use).py` | DCFRegime |
| Sentiment | `Sentiment_Hub/sentiment_etl (public use).py` | SentimentRegime |
| Liquidity | `Liquidity_Hub/liquidity_etl (public use).py` | LiquidityRegime |
| Macro Regime | `Macro_Inflation_Watch/etl (public use).py` | MacroRegime |

**Run tests at any time:**
```
python -m pytest tests/ -v
```
All 170 tests must stay green as you make changes.

---

## Priority 1 — Security (Do First, Takes 30 Minutes)

### Move all credentials to environment variables

Every script hardcodes SQL Server credentials and API keys. This is the highest-risk item.

**Pattern to apply in every file:**
```python
# Before
SQL_SERVER   = "YOUR_SQL_SERVER"
SQL_PASSWORD = "YOUR_SQL_PASSWORD"
FRED_API_KEY = "YOUR_FRED_API_KEY"
RAPIDAPI_KEY = "YOUR_RAPIDAPI_KEY"

# After
import os
SQL_SERVER   = os.environ["SQL_SERVER"]
SQL_USER     = os.environ["SQL_USER"]
SQL_PASSWORD = os.environ["SQL_PASSWORD"]
FRED_API_KEY = os.environ["FRED_API_KEY"]
RAPIDAPI_KEY = os.environ["RAPIDAPI_KEY"]
```

**Create a `.env` file at project root (never commit this):**
```
SQL_SERVER=your_server_name
SQL_USER=macro_user
SQL_PASSWORD=your_password
FRED_API_KEY=your_fred_key
RAPIDAPI_KEY=your_rapidapi_key
DCF_DB_PATH=C:\path\to\sp500_prices.db
DCF_TICKERS_PATH=C:\path\to\sp500_tickers.csv
DCF_OUTPUT_PATH=C:\path\to\Stock_Data_Current.csv
```

**Load it at the top of each script:**
```python
from dotenv import load_dotenv
load_dotenv()
```

Add `python-dotenv` to `requirements-test.txt` and to a new `requirements.txt`.

**Files affected:** All 6 scripts.

**Also fix the two hardcoded absolute paths in DCF scripts:**
```python
# calculate_dcf (public use).py and fetch_fundamentals_rapidapi (public use).py
DB_PATH      = os.environ.get("DCF_DB_PATH", r".\sp500_prices.db")
OUTPUT_PATH  = os.environ.get("DCF_OUTPUT_PATH", r".\Stock_Data_Current.csv")
TICKERS_PATH = os.environ.get("DCF_TICKERS_PATH", r".\sp500_tickers.csv")
```

Add `.env` to `.gitignore` if not already there.

---

## Priority 2 — Data Integrity (Prevent Data Loss)

### Fix the COT TRUNCATE-before-INSERT race condition

**File:** `COT_Hub/cot_etl - public use.py`
**Functions:** `upsert_raw_cot` (line ~548), `build_cot_master` (line ~651)

Currently: `TRUNCATE` commits immediately. If the INSERT below fails, the table is empty.

```python
# Before — unsafe
cursor.execute("TRUNCATE TABLE raw_cot")
conn.commit()
# ... if anything here fails, table is now permanently empty

# After — wrap both operations in one transaction
cursor.execute("BEGIN TRANSACTION")
cursor.execute("TRUNCATE TABLE raw_cot")
cursor.executemany(merge_sql, rows)
conn.commit()
# On failure, call conn.rollback() in the except block
```

Apply the same pattern to the `cot_weekly` truncate in `build_cot_master`.

Full pattern:
```python
def upsert_raw_cot(conn, df):
    if df.empty:
        return
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN TRANSACTION")
        cursor.execute("TRUNCATE TABLE raw_cot")
        rows = [tuple(row[c] for c in cols) for _, row in df[cols].iterrows()]
        cursor.executemany(sql, rows)
        conn.commit()
        log.info(f"raw_cot rebuilt: {len(rows)} rows")
    except Exception:
        conn.rollback()
        log.error("raw_cot upsert failed — rolled back to previous state")
        raise
```

### Fix the Macro ETL DELETE + APPEND non-atomic pattern

**File:** `Macro_Inflation_Watch/etl (public use).py`
**Functions:** `load_raw_cpi`, `load_raw_single`, `load_raw_spx`, `build_master`

Currently: `DELETE FROM table` and `df.to_sql(...append...)` are two separate transactions.

```python
# Before — two separate transactions, table left empty on failure
with engine.begin() as conn:
    conn.execute(text("DELETE FROM raw_cpi"))
df_out.to_sql("raw_cpi", engine, if_exists="append", index=False)

# After — both in one transaction
with engine.begin() as conn:
    conn.execute(text("DELETE FROM raw_cpi"))
    df_out.to_sql("raw_cpi", conn, if_exists="append", index=False)
```

The key is passing the active connection object `conn` to `to_sql` instead of `engine`, so both statements share the same transaction.

---

## Priority 3 — Known Production Bug (Silent Data Loss)

### Fix leading-zero CFTC codes being dropped silently

**File:** `COT_Hub/cot_etl - public use.py`
**Functions:** `parse_legacy_zip`, `parse_disagg_zip`

**Affected instruments:** ZN (043602), ZB (020601), 6E (099741)

**Root cause:** `pd.read_csv` infers dtype for numeric-looking columns. "043602" is read as integer `43602`, losing the leading zero. After `.astype(str)`, the code becomes "43602" which is not in `INSTRUMENTS`, so the row is silently dropped. No warning is emitted.

**Fix:** Force the CFTC code column to string dtype at read time.

```python
# In parse_legacy_zip and parse_disagg_zip, change:
df = pd.read_csv(f, low_memory=False)

# To:
df = pd.read_csv(f, low_memory=False, dtype=str)
```

**Verification:** The test `test_leading_zero_code_silently_dropped` in `tests/test_cot_etl.py` currently asserts `df.empty` (documenting the bug). After applying the fix, change that assertion to:
```python
assert not df.empty  # Bug fixed — ZN/ZB/6E now parse correctly
assert df.iloc[0]["symbol"] == "ZN"
```

**Impact:** This bug has been silently dropping ZN, ZB, and 6E rows from every COT ETL run since the beginning. After fixing, the first run will populate these instruments' history for the first time.

---

## Priority 4 — Reliability (Add Retry Logic)

### Add exponential backoff to all external fetches

Currently only `etl (public use).py` has retry logic, and it uses a flat 30-second wait.

**Create a shared utility** (see Priority 6 for the full shared module plan). For now, add this helper to each script:

```python
import time

def fetch_with_retry(fn, max_attempts=3, base_wait=5):
    """Retry an API call with exponential backoff."""
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            wait = base_wait * (2 ** attempt)  # 5s, 10s, 20s
            log.warning(f"Attempt {attempt+1} failed: {e}. Retrying in {wait}s...")
            time.sleep(wait)
```

**Usage example in `liquidity_etl`:**
```python
walcl = fetch_with_retry(lambda: fetch_fred("WALCL", FRED_START, FRED_END))
```

**Files to update:** `cot_etl`, `liquidity_etl`, `sentiment_etl`, `fetch_fundamentals_rapidapi`.

**For `cot_etl` specifically** — the ZIP downloads currently silently return `None` on failure. Add retry:
```python
def download_zip(url):
    return fetch_with_retry(
        lambda: _do_download(url),
        max_attempts=3,
        base_wait=10
    )
```

---

## Priority 5 — Performance (Fix Row-by-Row SQL)

### Replace row-by-row upserts with batch operations

The three biggest offenders. Each currently makes one SQL round-trip per row.

#### sentiment_etl — 7,000+ rows × 1 query each

```python
# Before — one IF EXISTS/UPDATE/INSERT per calendar day
for idx_date, row in df.iterrows():
    cursor.execute("IF EXISTS ... UPDATE ... ELSE INSERT ...", ...)

# After — bulk using a staging temp table approach
cursor.execute("""
    CREATE TABLE #stg_sentiment (
        date DATE, vix_close FLOAT, ... 
    )
""")
rows = [(d, n(row["vix_close"]), ...) for d, row in df.iterrows()]
cursor.executemany("INSERT INTO #stg_sentiment VALUES (?,?,?,...)", rows)
cursor.execute("""
    MERGE sentiment_daily AS target
    USING #stg_sentiment AS source ON target.date = source.date
    WHEN MATCHED THEN UPDATE SET ...
    WHEN NOT MATCHED THEN INSERT ...
""")
conn.commit()
```

#### liquidity_etl — same pattern for 3 staging tables

Same staging temp table approach as above, applied to `stg_FedBalanceSheet`, `stg_CreditSpreads`, and `stg_MoneyMarket`.

#### calculate_dcf — vectorise the DCF loop

```python
# Before — row-by-row Python loop
for _, row in df.iterrows():
    base_total = calculate_dcf(fcf, growth_base, discount_base)
    ...

# After — numpy broadcasting for the PV sum
def calculate_dcf_vectorised(fcf_series, growth, discount, terminal=0.025, years=5):
    t = np.arange(1, years + 1)
    # Each row of fcf_series broadcasts against t
    pv_sum = (fcf_series.values[:, None] * (1 + growth) ** t / (1 + discount) ** t).sum(axis=1)
    fcf_terminal = fcf_series * (1 + growth) ** years
    tv = (fcf_terminal * (1 + terminal)) / (discount - terminal)
    pv_tv = tv / (1 + discount) ** years
    return pd.Series(pv_sum + pv_tv, index=fcf_series.index)
```

---

## Priority 6 — Maintainability (Eliminate Duplication)

### Create a shared `etl_utils.py`

The same patterns appear copy-pasted across 5 scripts. Consolidate into one file:

**Create:** `etl_utils.py` at project root

```python
"""Shared utilities for all Market Intelligence Suite ETL pipelines."""

import os
import time
import logging
import numpy as np
import pyodbc

log = logging.getLogger(__name__)


def get_conn(server, database, user, password,
             driver="ODBC Driver 17 for SQL Server"):
    """Return a pyodbc connection using a context-manager-safe wrapper."""
    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={user};"
        f"PWD={password};"
        "TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str)


def fetch_with_retry(fn, max_attempts=3, base_wait=5):
    """Call fn(), retrying with exponential backoff on failure."""
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            wait = base_wait * (2 ** attempt)
            log.warning(f"Attempt {attempt + 1} failed: {e}. Retry in {wait}s")
            time.sleep(wait)


def safe_int(val):
    """Convert to Python int; return None for NaN/inf/None/empty."""
    if val is None:
        return None
    if hasattr(val, "item"):
        val = val.item()
    if isinstance(val, float):
        if np.isnan(val) or np.isinf(val):
            return None
        return int(val)
    try:
        cleaned = str(val).replace(",", "").strip()
        if cleaned.lower() in ("nan", "inf", "-inf", "none", ""):
            return None
        return int(float(cleaned))
    except Exception:
        return None


def safe_float(val, decimals=4):
    """Convert to Python float rounded to decimals; return None on failure."""
    try:
        f = float(val)
        if np.isnan(f) or np.isinf(f):
            return None
        return round(f, decimals)
    except Exception:
        return None


def configure_logging(log_file, level=logging.INFO):
    """Standard logging setup for all ETL scripts."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
```

**Then in each script, replace the duplicated helpers:**
```python
# Before (each file has its own copies)
def _safe_int(val): ...
def _safe_float(val): ...

# After
from etl_utils import safe_int, safe_float, get_conn, fetch_with_retry
```

### Extract `classify_regime` to module level in macro ETL

Currently a nested closure inside `build_master`, making it untestable.

```python
# Move outside build_master so it can be imported and tested directly
def classify_regime(cpi_smoothed, gdp_smoothed):
    if pd.isna(cpi_smoothed) or pd.isna(gdp_smoothed):
        return None, None
    if   cpi_smoothed < 3.0  and gdp_smoothed >= 2.0: return "Goldilocks",  1
    elif cpi_smoothed >= 3.0 and gdp_smoothed >= 2.0: return "Inflation",   2
    elif cpi_smoothed >= 3.0 and gdp_smoothed < 2.0:  return "Stagflation", 3
    else:                                               return "Recession",   4
```

Then in `build_master`, call it as:
```python
df[["regime_label", "regime_code"]] = df.apply(
    lambda r: pd.Series(classify_regime(r["cpi_smoothed"], r["gdp_smoothed"])),
    axis=1
)
```

After this change, update `tests/test_macro_etl.py` to import the function directly instead of replicating it.

---

## Priority 7 — Reproducibility (Dependency Management)

### Create `requirements.txt` with pinned versions

Check your installed versions with `pip freeze`, then pin them:

```
# requirements.txt
requests==2.31.0
pandas==2.1.4
numpy==1.26.2
pyodbc==5.0.1
sqlalchemy==2.0.23
fredapi==0.5.1
yfinance==0.2.36
python-dotenv==1.0.0
```

Determine your actual versions:
```
pip freeze > requirements.txt
```

Then trim to only the packages actually imported across the 6 scripts.

---

## Priority 8 — Code Safety (Connection Lifecycle)

### Use context managers for all DB connections

**Files:** `calculate_dcf`, `sentiment_etl`, `cot_etl`, `liquidity_etl`

```python
# Before — conn.close() never reached if an exception occurs above it
conn = get_conn()
cursor = conn.cursor()
cursor.execute(...)
conn.close()  # skipped on exception → connection leaked

# After
import contextlib

@contextlib.contextmanager
def managed_conn(server, database, user, password):
    conn = get_conn(server, database, user, password)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# Usage
with managed_conn(SQL_SERVER, SQL_DATABASE, SQL_USER, SQL_PASSWORD) as conn:
    upsert_raw_cot(conn, raw_df)
    build_cot_master(conn)
    create_views(conn)
```

Add `managed_conn` to `etl_utils.py`.

---

## Priority 9 — Maintainability (Centralise Magic Numbers)

### Move all thresholds to a `config.py`

Create `config.py` at project root:

```python
# config.py — all tunable thresholds in one place

# COT Hub
COT_ZSCORE_WINDOW   = 52    # weeks (1 year rolling)
COT_ZSCORE_MIN_PERIODS = 10
COT_POSITIONING_TIERS = {   # (lower_bound, label)  — checked top-to-bottom
    1.5:  "Extreme Long",
    0.5:  "Long",
    -0.5: "Neutral",
    -1.5: "Short",
}
COT_DIVERGENCE_THRESHOLD = 1.0  # |Z| > this triggers Bullish/Bearish signal

# Sentiment Hub
SENTIMENT_ZSCORE_WINDOW = 252   # trading days (~1 year)
SENTIMENT_ZSCORE_MIN_PERIODS = 60
SENTIMENT_TIERS = {
    1.5: "Extreme Greed",
    0.5: "Greed",
   -0.5: "Neutral",
   -1.5: "Fear",
}
SENTIMENT_FG_CLAMP = 3.0        # ±σ clamp before mapping to 0-100

# DCF Hub
DCF_TERMINAL_GROWTH   = 0.025
DCF_PROJECTION_YEARS  = 5
DCF_BUY_THRESHOLD     = 0.10   # gap > 10% → BUY
DCF_SELL_THRESHOLD    = -0.10  # gap < -10% → SELL

# Macro Hub
MACRO_GOLDILOCKS_CPI_MAX = 3.0  # CPI smoothed < this → not inflationary
MACRO_GOLDILOCKS_GDP_MIN = 2.0  # GDP smoothed >= this → not recessionary
MACRO_GDP_SMOOTH_WINDOW  = 6    # months
MACRO_CPI_SMOOTH_WINDOW  = 3    # months

# Liquidity Hub
LIQUIDITY_FRED_START = "2002-01-01"
LIQUIDITY_DIM_DATE_END = "2035-12-31"
```

Import into each script:
```python
from config import COT_ZSCORE_WINDOW, COT_POSITIONING_TIERS, ...
```

---

## Priority 10 — Date Correctness (Timezone Normalisation)

### Normalise all dates to US/Eastern close-of-business

**Files:** `cot_etl`, `sentiment_etl`

CBOE data is US Eastern. FRED data is UTC-normalised. COT dates are parsed as naive `date` objects. Cross-dataset joins on `date` can be off by one day for series that report after market close.

```python
# In fetch_cboe_putcall (sentiment_etl):
# After parsing:
df_archive["date"] = pd.to_datetime(
    df_archive["date"], format="%m/%d/%Y", errors="coerce"
).dt.tz_localize("America/New_York").dt.normalize().dt.date

# For FRED (already UTC-normalised daily close, no change needed)
# For COT (CFTC reports Friday 3:30 PM ET, so date is always correct as-is)
```

In practice for this system: all FRED series are already daily close values and date-correct. The main risk is the CBOE archive join — normalising that to date-only (which is already done via `.dt.date`) is sufficient. Add a data-quality assertion in `build_sentiment_master` to detect date gaps larger than 5 business days:

```python
date_gaps = pd.Series(sorted(df.index)).diff().dt.days.dropna()
if (date_gaps > 7).any():
    log.warning(f"Large date gap detected in sentiment data: max gap = {date_gaps.max():.0f} days")
```

---

## Testing Checklist

After each fix, verify:

```
python -m pytest tests/ -v
```

Specific tests to update after certain fixes:

| Fix | Test to update |
|---|---|
| Fix leading-zero CFTC code bug (Priority 3) | `test_leading_zero_code_silently_dropped` — flip `assert df.empty` to `assert not df.empty` |
| Extract `classify_regime` to module level (Priority 6) | `test_macro_etl.py` — replace the replicated function with a direct import |
| Add `etl_utils.py` (Priority 6) | Update `conftest.py` fixtures to also load `etl_utils` if needed |

---

## Suggested Fix Order (Week by Week)

### Week 1 — Security + Data Integrity
- [ ] Move all credentials to `.env` + `os.environ` (Priority 1)
- [ ] Wrap COT truncate+insert in single transaction (Priority 2)
- [ ] Wrap Macro DELETE+append in single transaction (Priority 2)
- [ ] Create `requirements.txt` (Priority 7)

### Week 2 — Bug Fixes + Reliability
- [ ] Fix leading-zero CFTC code bug (Priority 3)
- [ ] Add retry with exponential backoff to all external fetches (Priority 4)
- [ ] Add context managers to all DB connections (Priority 8)

### Week 3 — Shared Utilities + Performance
- [ ] Create `etl_utils.py` and migrate all scripts to use it (Priority 6)
- [ ] Replace row-by-row upserts with batch operations (Priority 5)
- [ ] Vectorise DCF calculation loop (Priority 5)

### Week 4 — Cleanup
- [ ] Extract `classify_regime` to module level (Priority 6)
- [ ] Move all magic numbers to `config.py` (Priority 9)
- [ ] Add timezone assertion to sentiment ETL (Priority 10)
- [ ] Run full test suite and verify 170+ tests pass

---

## Reference: What Each Test File Covers

| File | What breaks if tests fail |
|---|---|
| `tests/test_cot_etl.py` | Safe scalar conversion, positioning labels, Z-score math, CFTC URL format, ZIP parsing, consolidated ID resolution |
| `tests/test_calculate_dcf.py` | DCF formula correctness, per-share conversion, BUY/HOLD/SELL thresholds, quality tier logic across all sectors |
| `tests/test_sentiment_etl.py` | Sentiment labels, Z-score math, synthetic Fear & Greed formula (boundaries + monotonicity), composite Z averaging |
| `tests/test_liquidity_etl.py` | Unit conversions (M→B, pct→bps), Fed BS spine merge, credit spread outer join, money market left join |
| `tests/test_macro_etl.py` | YoY/MoM % change math, monthly resampling, all 4 regime boundaries and NaN edge cases |
| `tests/test_fetch_fundamentals.py` | Yahoo Finance JSON extraction, D/E ratio conversion, resume/idempotency via in-memory SQLite |
