# Changelog

All notable changes to the Market Intelligence Suite are documented here.

---

## [1.1.0] — 2026-06-22

### Added
- `etl_utils.py` — shared utility module; `managed_conn`, `fetch_with_retry`,
  `safe_int`, `safe_float`, `configure_logging` centralised and imported by all
  6 ETL scripts instead of duplicated across each file
- `config.py` — all magic numbers in one place: COT Z-score window (52w),
  Macro regime thresholds (3.0% / 2.0%), DCF buy/sell thresholds (±10%),
  Sentiment clamp (±3σ), Liquidity composite weights
- `tests/` — 177-test pytest suite covering all 6 ETL modules; all passing
- `requirements.txt` — pinned production dependencies (8 packages)
- `requirements-test.txt` — test-only dependencies
- `FIX_GUIDE.md` and `TECHNICAL_DEBT.md` — documented issue inventory and
  fix instructions for future sessions

### Fixed
- **Credentials** — all hardcoded SQL Server, FRED, and RapidAPI secrets
  removed from source; all scripts now load from `.env` via `python-dotenv`.
  `.env` excluded from repo via `.gitignore`
- **COT ETL — transaction safety** — `upsert_raw_cot` and `build_cot_master`
  now wrap TRUNCATE + INSERT in `BEGIN TRANSACTION / ROLLBACK`; a failed insert
  rolls back to the previous table state instead of leaving an empty table.
  TRUNCATE in `build_cot_master` moved to after data computation so a compute
  failure never touches the live table
- **COT ETL — leading-zero CFTC codes** — `pd.read_csv(dtype=str)` now
  preserves codes like `043602` (ZN), `020601` (ZB), `099741` (6E) that integer
  parsing silently dropped. ZN, ZB, and 6E will populate on the next ETL run
  for the first time
- **COT ETL** — `_safe_int` and `_safe_float` handle commas, `nan`/`inf`
  string literals, and numpy scalar `.item()` unwrap
- **Macro ETL — atomic DELETE + append** — all `load_raw_*` functions now pass
  an active SQLAlchemy connection (not the engine) to `df.to_sql()` so DELETE
  and INSERT share one transaction
- **Macro ETL** — `classify_regime` extracted from nested closure inside
  `build_master` to module level; now directly importable and testable
- **Sentiment ETL — bulk upsert** — ~7,000 row-by-row IF EXISTS queries
  replaced with staging temp table + single MERGE (~50× fewer SQL round-trips)
- **Sentiment ETL** — `classify_sentiment` boundary conditions corrected;
  `fg_synthetic` clamps composite Z to ±3σ before mapping to 0–100
- **Liquidity ETL** — `apply_unit_conversion` handles unknown conversion types
  gracefully instead of raising `KeyError`
- **All scripts — exponential backoff retry** — `fetch_with_retry()` added to
  all 5 scripts making external calls (FRED, CFTC ZIPs, CBOE, CNN, RapidAPI);
  retries up to 3 times: 5s → 10s → 20s. Macro ETL's flat 30s sleep upgraded
  to the same pattern
- **All scripts — connection context managers** — `managed_conn()` replaces
  bare `pyodbc.connect` in COT, Liquidity, Sentiment, and DCF scripts; commits
  on clean exit, rolls back on exception, always closes

### Changed
- **DCF ETL** — `run_calculations()` rewritten with numpy broadcasting;
  `iterrows()` over 503 tickers replaced (~15× speedup on compute stage)
- All 6 ETL scripts migrated to import shared helpers from `etl_utils`;
  local duplicate implementations removed

---

## [1.0.0] — 2026-04-29

### Added
- COT Positioning Dashboard (COTRegime) — 12 futures instruments, 52-week
  rolling Z-scores, Bullish/Bearish Divergence signals. Friday 6 PM refresh
- Market Sentiment Dashboard (SentimentRegime) — VIX, CBOE Put/Call ratio,
  CNN Fear & Greed composite (AAII evaluated and excluded — no automatable
  free feed). Saturday 5:30 AM refresh
- Liquidity Regime Dashboard (LiquidityRegime) — FRED-based Fed BS/TGA/RRP
  decomposition, HY Z-score, yield curve composite. Saturday 8 PM refresh
- Macro Regime Dashboard (MacroRegime) — GDP/CPI/unemployment/PMI composite
  scoring, four-regime classification. Sunday 5 AM refresh
- DCF Valuation Dashboard (DCFRegime) — 503 S&P 500 stocks, Three Pillars
  framework, three-scenario stress testing. Sunday 5 AM refresh
- Backtest Dashboard (BacktestRegime) — 4,483 trades across 13 instruments,
  Confluence Score validation (46% win rate at Score 3 vs. 24% at Score 0–1)
- Trading Confluence Panel on Liquidity Dashboard joining signals from all
  five dashboards into per-instrument bias table (`vw_ConfluenceSignals`)
- Full automation via Windows Task Scheduler + Power BI Personal Gateway
  (Personal Gateway on DESKTOP-1CRNFTD)
- Weekly brief auto-generation via `generate_brief.py` → HTML + Word → Gmail
  SMTP, every Monday 5 AM

### Architecture
- Python ETL pipelines → SQL Server 2019 (DESKTOP-1CRNFTD), separate database
  per dashboard, all transform logic in SQL views (no DAX data shaping)
- Power BI Desktop/Service with Import mode and scheduled refresh
- Post-upsert data validation with null rate checks on every ETL cycle
- `vw_ConfluenceSignals` cross-database view using OUTER APPLY cadence-mismatch
  pattern to join four dashboards operating on different data cadences
