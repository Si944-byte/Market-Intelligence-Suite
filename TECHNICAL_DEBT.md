# Technical Debt — Market Intelligence Suite

*Audited: 2026-06-22 | Scope: All 6 ETL pipeline scripts*

---

## Critical

### 1. Hardcoded Secrets
**Files:** All 6 scripts

`SQL_PASSWORD`, `FRED_API_KEY`, and `RAPIDAPI_KEY` are embedded directly in source code. Two DCF scripts also hardcode absolute Windows paths including the username.

```python
# Current (all scripts)
SQL_PASSWORD = "YOUR_SQL_PASSWORD"
FRED_API_KEY = "YOUR_FRED_API_KEY"
DB_PATH = r"C:\Users\TJs PC\OneDrive\Desktop\Projects\DCF Models\sp500_prices.db"

# Fix
import os
SQL_PASSWORD = os.getenv("SQL_PASSWORD")
FRED_API_KEY = os.getenv("FRED_API_KEY")
DB_PATH = os.getenv("DCF_DB_PATH", "./sp500_prices.db")
```

---

### 2. No Tests
**Files:** All 6 scripts

Zero test files exist anywhere in the project. No unit, integration, or regression coverage. Any refactor or threshold change is unvalidated until it hits the live database.

**Missing coverage:**
- `parse_cot_date()` edge cases (6-digit vs 8-digit vs ISO)
- Z-score calculations and rolling window correctness
- Regime classification thresholds
- SQL upsert correctness (duplicate handling)
- API response parsing (nested JSON paths)

---

### 3. Truncate Before Insert (Data Loss Risk)
**File:** `COT_Hub/cot_etl - public use.py` — Lines 552–555, 651–654

`TRUNCATE TABLE` runs before the new data insert is confirmed. If the subsequent insert fails, the database is left empty with no rollback.

```python
# Current — dangerous
cursor.execute("TRUNCATE TABLE raw_cot")
conn.commit()
# ... if anything below fails, table is now empty

# Fix — wrap in a single transaction
try:
    cursor.execute("TRUNCATE TABLE raw_cot")
    cursor.executemany(insert_sql, rows)
    conn.commit()
except Exception:
    conn.rollback()
    raise
```

---

## High Severity

### 4. No Idempotency / Atomic Transactions
**Files:** `liquidity_etl`, `sentiment_etl`, `etl (Macro)`

- `liquidity_etl` and `sentiment_etl`: Row-by-row MERGE/IF-EXISTS upserts — a mid-loop failure commits partial rows with no rollback.
- `etl (Macro)`: `DELETE FROM raw_cpi` followed by `df.to_sql(..., if_exists="append")` are two separate statements. A crash between them leaves an empty staging table.

**Fix:** Batch all operations within a single `BEGIN TRANSACTION / COMMIT / ROLLBACK` block, or use `executemany()` with a single commit at the end.

---

### 5. Silent API Failure Propagation
**Files:** `fetch_fundamentals_rapidapi`, `cot_etl`, `sentiment_etl`

Bare `except` blocks swallow errors and return `None`, allowing the pipeline to continue with missing data that corrupts downstream calculations.

```python
# fetch_fundamentals_rapidapi ~line 100 — silent failure
except:
    return None

# cot_etl — logs warning but continues with incomplete data
except Exception as e:
    log.warning(f"Download failed: {url} — {e}")
    return None
```

Only `etl (Macro)` has retry logic, but it uses a flat 30-second wait with no exponential backoff and no distinction between retryable (timeout) and non-retryable (bad API key) errors.

**Fix:** Add retry with exponential backoff to all external fetches. Fail fast (raise) on non-retryable errors rather than returning `None`.

---

### 6. Fragile Data Parsing
**Files:** `cot_etl`, `fetch_fundamentals_rapidapi`, `sentiment_etl`

- **`cot_etl`:** Column detection tries a fixed list of candidate names. Any whitespace change in CFTC's CSV silently returns nulls for the entire column.
- **`fetch_fundamentals_rapidapi`:** D/E correction `if de > 20: de / 100` is a magic heuristic — if the API changes its unit convention, values are silently wrong.
- **`sentiment_etl`:** `pd.to_datetime(..., errors="coerce")` silently converts unparseable CBOE dates to `NaT` and drops them with no logging of how many rows were lost.
- **`calculate_dcf`:** 30-parameter `INSERT` tuple — a column order mismatch silently writes wrong values to wrong columns.

---

### 7. Schema Drift / No Migrations
**Files:** All 5 SQL-writing scripts

Raw `CREATE TABLE` DDL and column lists are hardcoded as Python strings. Adding or renaming a column requires manually updating multiple Python files. No migration tool (Alembic, Flyway) is used, and there is no rollback path.

---

## Medium Severity

### 8. Code Duplication
**Files:** `liquidity_etl`, `sentiment_etl`, `cot_etl`, `fetch_fundamentals_rapidapi`, `etl (Macro)`

The same patterns are copy-pasted across scripts with no shared utilities:

| Pattern | Duplicated In |
|---|---|
| MERGE / IF-EXISTS upsert | `liquidity_etl` ×3, `sentiment_etl` ×3, `cot_etl` |
| `raw()` / `safe_float()` helpers | `fetch_fundamentals_rapidapi`, `calculate_dcf` |
| Date parsing | `cot_etl` legacy parser, `cot_etl` disagg parser |
| Delete → clean → append | All `load_raw_*` functions in `etl (Macro)` |

**Fix:** Extract a shared `etl_utils.py` with a generic upsert helper, retry wrapper, and connection context manager.

---

### 9. Row-by-Row SQL (Performance)
**Files:** `sentiment_etl`, `liquidity_etl`, `cot_etl`, `calculate_dcf`

- `sentiment_etl`: One `IF EXISTS / UPDATE / INSERT` round-trip per calendar day — 7,000+ rows, one query each.
- `liquidity_etl`: One MERGE per row per staging table.
- `cot_etl`: `df.iterrows()` to build result rows instead of vectorized operations.
- `calculate_dcf`: 500+ ticker DCF loop in pure Python with no numpy vectorization.

**Fix:** Use `cursor.executemany()` for bulk inserts, or stage to a temp table and merge in one SQL statement.

---

### 10. Connection Lifecycle / Leaks
**Files:** `calculate_dcf`, `sentiment_etl`, `cot_etl`, `liquidity_etl`

`conn.close()` is called at the end of function bodies — any exception before it leaks the connection. Only `etl (Macro)` uses `engine.begin()` as a context manager correctly.

Additionally, 4 of 5 scripts use raw `pyodbc` while `etl (Macro)` uses `sqlalchemy` — no shared abstraction.

```python
# Current — connection leaks on exception
conn = get_conn()
cursor.execute(...)
conn.close()  # never reached if above raises

# Fix
with pyodbc.connect(...) as conn:
    cursor = conn.cursor()
    cursor.execute(...)
```

---

### 11. Missing Structured Logging
**Files:** `fetch_fundamentals_rapidapi`, `etl (Macro)`, `liquidity_etl`, `sentiment_etl`

- `fetch_fundamentals_rapidapi` and `etl (Macro)` use `print()` with no timestamps or log levels.
- `liquidity_etl` and `sentiment_etl` use the `logging` module but include emoji (`✅`, `❌`) that break in non-UTF-8 terminals and make log parsing unreliable.
- No structured (JSON) output for alerting or log aggregation.

---

### 12. Magic Numbers / Unexplained Thresholds

All thresholds are hardcoded in logic with no documentation or central config.

| Constant | File | Value |
|---|---|---|
| Z-score rolling window | `cot_etl` | 52 weeks |
| Z-score min periods | `cot_etl` | 10 |
| COT positioning tiers | `cot_etl` | ±0.5, ±1.5 |
| Sentiment tiers | `sentiment_etl` | ±0.5, ±1.5 |
| Synthetic F&G clamp | `sentiment_etl` | ±3σ → 0–100 |
| D/E correction threshold | `fetch_fundamentals_rapidapi` | >20 → divide by 100 |
| BUY/SELL signal gap | `calculate_dcf` | ±10% |
| DCF projection years | `calculate_dcf` | 5 |
| Terminal growth rate | `calculate_dcf` | 2.5% |
| DimDate range | `liquidity_etl` | 2002–2035 |
| FRED history start | `liquidity_etl` | 2002-01-01 |

**Fix:** Consolidate into a `config.py` or top-of-file `CONFIG` dict so thresholds can be adjusted without touching logic.

---

### 13. No Dependency Management

No `requirements.txt`, `pyproject.toml`, or virtual environment exists. Package versions are unknown and the environment is not reproducible.

**Inferred dependencies (all unpinned):**

```
requests
pandas
numpy
pyodbc
sqlalchemy      # etl (Macro) only
fredapi         # etl (Macro) only; others use raw HTTP
yfinance        # etl (Macro) only
```

**Fix:** Create `requirements.txt` with pinned versions. Standardise on either `pyodbc` or `sqlalchemy` — not both.

---

### 14. Timezone-Unaware Dates
**Files:** `cot_etl`, `sentiment_etl`

CBOE data is US Eastern, FRED data is UTC-normalized, and COT dates are parsed as naive `.date()` objects. Cross-dataset date joins can silently be off by one day for any series that reports after market close.

---

## Priority Fix Order

| Priority | Fix | Impact |
|---|---|---|
| 1 | Secrets → `os.getenv()` + `.env` file | Security |
| 2 | Wrap truncate+insert in single transaction with rollback | Data integrity |
| 3 | Add retry + exponential backoff to all external fetches | Reliability |
| 4 | Replace row-by-row upserts with `executemany()` | Performance |
| 5 | Extract shared `etl_utils.py` (connection manager, upsert, retry) | Maintainability |
| 6 | Add `requirements.txt` with pinned versions | Reproducibility |
| 7 | Write integration tests for Z-score logic and SQL upserts | Correctness |
| 8 | Move all thresholds to `config.py` | Maintainability |
| 9 | Standardise on `sqlalchemy` + context managers | Connection safety |
| 10 | Add timezone normalisation on all date parsing | Date correctness |
