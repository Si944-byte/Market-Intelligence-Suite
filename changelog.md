# Changelog

All notable changes to the Market Intelligence Suite are documented here.

---

## [1.2.0] — 2026-06-22

### Added
- `etl_utils.py` — shared utility module; `managed_conn`, `fetch_with_retry`,
  `safe_int`, `safe_float`, `configure_logging` centralised and imported by all
  6 ETL scripts instead of duplicated across each file
- `config.py` — all magic numbers in one place: COT Z-score window (52w),
  Macro regime thresholds (3.0% / 2.0%), DCF buy/sell thresholds (±10%),
  Sentiment clamp (±3σ), Liquidity composite weights

### Changed
- **Sentiment ETL** — row-by-row upsert (~7,000 individual IF EXISTS queries)
  replaced with staging temp table + single MERGE (~50× fewer SQL round-trips)
- **DCF ETL** — `run_calculations()` rewritten with numpy broadcasting; no more
  `iterrows()` over 503 stocks (~15× speedup on the compute stage)
- **Macro ETL** — `classify_regime` extracted from nested closure inside
  `build_master` to module level; now directly importable and testable
- All 6 ETL scripts migrated to import from `etl_utils`; local duplicate
  implementations of `get_conn`, `managed_conn`, `fetch_with_retry` removed

### Tests
- 170 → **177 tests** (+7 new `classify_regime` boundary tests)

---

## [1.1.0] — 2026-06-22

### Fixed
- **COT ETL** — `_safe_int` now strips commas before parsing; handles `nan`/`inf`
  string literals; numpy scalar `.item()` unwrap prevents overflow on large CFTC
  positions
- **COT ETL** — `_safe_float` rounds to 4 dp, unwraps numpy scalars; consistent
  with downstream Power BI display precision
- **COT ETL** — retry loop uses `time.sleep` correctly; `fetch_with_retry` wraps
  all CFTC HTTP calls; transient 5xx errors no longer abort a full run
- **COT ETL** — `managed_conn` context manager replaces bare `pyodbc.connect`;
  all DB writes are atomic; partial runs roll back cleanly
- **Liquidity ETL** — `apply_unit_conversion` handles unknown `conversion_type`
  gracefully; preserves raw value instead of raising `KeyError`
- **Sentiment ETL** — `classify_sentiment` boundary conditions corrected:
  `|Z| >= 1.5` → Extreme; `|Z| >= 0.5` → Greed/Fear; else Neutral
- **Sentiment ETL** — `fg_synthetic` clamps composite Z to ±3σ before mapping
  to 0–100; extreme readings no longer produce values outside that range

### Tests
- **170 tests** added (pytest suite); all passing on clean install

---

## [1.0.0] — April 2026

### Added
- Liquidity Regime Dashboard (LiquidityRegime) — FRED-based Fed BS/TGA/RRP
  decomposition, HY Z-score, yield curve composite. Saturday 8 PM refresh.
- COT Positioning Dashboard (COTRegime) — 12 futures instruments, 52-week
  rolling Z-scores, Bullish/Bearish Divergence signals. Friday 6 PM refresh.
- Macro Regime Dashboard (MacroRegime) — GDP/CPI/unemployment/PMI composite
  scoring. Sunday 5 AM refresh.
- DCF Valuation Dashboard (DCFRegime) — 503 S&P 500 stocks, Three Pillars
  framework, stress testing. Sunday 5 AM refresh.
- Market Sentiment Dashboard (SentimentRegime) — CBOE P/C, CNN Fear & Greed,
  AAII composite. Saturday 5:30 AM refresh.
- Full automation via Windows Task Scheduler + Power BI Personal Gateway
- Trading Confluence Panel on Liquidity Dashboard pulling signals from all
  four dashboards into per-instrument bias table

### Architecture
- Python ETL pipelines for all five dashboards
- SQL Server 2019 local instance with separate database per dashboard
- Power BI Desktop/Service with scheduled refresh
- Post-upsert data validation with null rate checks on every cycle