# Roadmap — Market Intelligence Suite

## Current State

The suite is fully operational across all five hubs. Five ETL pipelines run on a weekly Task Scheduler cadence, each writing to isolated SQL Server databases that feed Power BI dashboards. The system has been live and producing trading signals.

**What exists and works:**
- All five ETL pipelines running end-to-end
- 503-stock DCF valuation engine with 3 scenarios and quality tiers
- 12-instrument COT positioning with 52-week Z-scores and divergence signals
- Composite sentiment score with synthetic Fear & Greed back to 2006
- Macro regime classification (Goldilocks / Inflation / Stagflation / Recession)
- Liquidity regime with Fed balance sheet decomposition
- Trading Confluence Panel joining all five signals
- 170-test suite covering all core business logic
- Architecture, data flow, and technical debt documentation

**Known gaps (from audit):**
- Credentials are placeholder strings, not environment variables
- No shared utilities — same patterns copy-pasted across 6 scripts
- Row-by-row SQL operations (performance)
- No retry logic on most API calls
- ZN/ZB/6E silently dropped due to leading-zero bug in CFTC parsing
- COT truncate-before-insert has data loss risk on failure

See `TECHNICAL_DEBT.md` and `FIX_GUIDE.md` for the full inventory and exact fix instructions.

---

## Phase 1 — Stabilise (Weeks 1–4)

Goal: Make the existing system production-safe. No new features — only fixes.

### Week 1 — Security and Data Integrity

- [ ] **Credentials to environment variables** across all 6 scripts
  - Create `.env` file + `python-dotenv` loading
  - Add `.env` to `.gitignore`
  - Replace all hardcoded paths in DCF scripts with `os.environ.get()`

- [ ] **Fix COT truncate-before-insert data loss**
  - Wrap TRUNCATE + INSERT in a single transaction with rollback
  - Same fix for `cot_weekly` truncate in `build_cot_master`

- [ ] **Fix Macro ETL DELETE + append gap**
  - Pass active connection to `to_sql()` so both statements share one transaction
  - Apply to all 7 load functions (`load_raw_cpi`, `load_raw_single` ×5, `build_master`)

- [ ] **Create `requirements.txt`**
  - Pin all 8 packages to current installed versions

### Week 2 — Bug Fixes and Reliability

- [ ] **Fix ZN/ZB/6E leading-zero bug in CFTC parsing**
  - Add `dtype=str` to `pd.read_csv()` in `parse_legacy_zip` and `parse_disagg_zip`
  - Update `test_leading_zero_code_silently_dropped` assertion from `assert df.empty` to `assert not df.empty`
  - First run after fix will populate ZN, ZB, 6E history for the first time

- [ ] **Add retry with exponential backoff to all external fetches**
  - COT ZIP downloads (currently silent fail)
  - Liquidity FRED calls (currently no retry)
  - Sentiment FRED + CBOE + CNN calls (currently no retry)
  - DCF RapidAPI calls (currently no retry)
  - Macro ETL already has retry — upgrade flat 30s wait to exponential (5s, 10s, 20s)

- [ ] **Add connection context managers**
  - Replace all `conn.close()` calls at end of function body
  - Use `try/except/finally` or a `managed_conn()` context manager in `etl_utils.py`

### Week 3 — Shared Utilities and Performance

- [ ] **Create `etl_utils.py`**
  - `safe_int()`, `safe_float()`, `get_conn()`, `fetch_with_retry()`, `managed_conn()`
  - `configure_logging()` — standardised handler setup
  - Update all 6 scripts to import from it

- [ ] **Replace row-by-row SQL with bulk operations**
  - Sentiment ETL: 7,000+ individual IF EXISTS/UPDATE/INSERT → staging temp table + MERGE
  - Liquidity ETL: per-row MERGE on 3 staging tables → same pattern
  - COT ETL: already uses `executemany` — verify no remaining per-row loops

- [ ] **Vectorise DCF calculation**
  - Replace `df.iterrows()` loop over 503 stocks with numpy broadcasting
  - Estimated speedup: 10-20× (from ~60s to ~3-6s for the compute stage)

### Week 4 — Cleanup and Verification

- [ ] **Extract `classify_regime` to module level** (macro ETL)
  - Currently a nested closure, untestable via direct import
  - Move outside `build_master`, update `test_macro_etl.py` to import directly

- [ ] **Create `config.py`** with all magic numbers
  - COT window (52), positioning thresholds (±0.5, ±1.5), divergence threshold (1.0)
  - Sentiment window (252), sentiment thresholds, F&G clamp (±3σ)
  - DCF signal thresholds (±10%), terminal growth (2.5%), projection years (5)
  - Macro regime boundaries (CPI 3.0%, GDP 2.0%)

- [ ] **Run full test suite — all 170 tests green**
- [ ] **Run each pipeline end-to-end once** after all changes
- [ ] **Tag v1.0.0** in git

---

## Phase 2 — Observability (Weeks 5–8)

Goal: Know immediately when something breaks, without checking Power BI.

### ETL Run Monitoring

- [ ] **Structured JSON logging** for all scripts
  - Replace `print()` and emoji-decorated `log.info()` with structured output
  - Fields: `run_id`, `hub`, `timestamp`, `status`, `rows_written`, `errors`
  - Enables log aggregation later (Windows Event Log, or a simple JSON file per run)

- [ ] **ETL summary email / notification on completion**
  - Simple: write a summary `.txt` file per run that Windows Task Scheduler can attach to an email trigger
  - Better: send via `smtplib` at end of `main()` — success vs. failure with row counts
  - Fields: run time, rows upserted per table, any warnings, latest data date

- [ ] **Data quality assertions in every pipeline**
  COT already has `validate_cot_weekly()` — extend this pattern to all hubs:
  - Row count vs. expected range (flag if < 90% of prior week)
  - Latest date within expected lag window (e.g. warn if liquidity data > 10 days old)
  - Null rate checks on critical columns (e.g. warn if Z-score null rate > 15%)
  - Cross-hub date alignment check (warn if COT and Sentiment latest dates differ > 7 days)

- [ ] **Pipeline run history table**
  Add a single `etl_runs` table to one of the databases (or a new `OpsLog` DB):
  ```
  etl_runs: run_id, hub, run_start, run_end, status, rows_written, error_message
  ```
  Each script writes one row on completion. Power BI can surface a "Last ETL Run" status card on each dashboard.

### Test Expansion

- [ ] **Add tests for CBOE archive CSV parsing** (synthetic CSV input)
- [ ] **Add tests for FRED JSON response parsing** (mock `requests.get`)
- [ ] **Add tests for the Macro `build_master` join logic** (mock `pd.read_sql`)
- [ ] **Add tests for `classify_regime` via direct import** (after Phase 1 refactor)
- [ ] **Add a test for DCF `run_calculations`** with a minimal DataFrame fixture
- [ ] **Target: 250+ tests**

---

## Phase 3 — Enhancement (Weeks 9–16)

Goal: Make the system smarter and faster without changing the core architecture.

### COT Hub

- [ ] **Incremental downloads instead of full rebuild**
  Currently re-downloads all years every Friday (~20 ZIPs, ~5 minutes).
  Only the current year's ZIP changes week-to-week. Cache previous years locally.
  ```
  Strategy: download deacot{year}.zip only if no local cache OR year == current_year
  Estimated speedup: from ~20 ZIP downloads to ~1-2 per run
  ```

- [ ] **Add Divergence Strength to vw_cot_latest**
  Already calculated as `|Spec Z| + |Comm Z|` in Python but not surfaced in the view.
  Add as a computed column so Power BI can sort by it directly.

- [ ] **Extend to additional instruments**
  Candidates: Silver Mini (YI), Copper (HG), Japanese Yen (6J), British Pound (6B), Canadian Dollar (6C)
  The CFTC parsing handles them — just add entries to `INSTRUMENTS` dict.

### DCF Hub

- [ ] **Financials sector: P/B and ROE treatment**
  Currently flagged in the README as "Phase 2". Banks and insurance companies have structurally different fundamentals — using Net Income as FCF proxy is a blunt workaround.
  Better: pull Price-to-Book ratio and Return on Equity. DCF for financials should be excess return model, not FCF-based.

- [ ] **Historical DCF tracking**
  Currently only stores the latest run. Add a date dimension so you can see how intrinsic value and signal have evolved for each ticker over time. Enables backtesting of signal accuracy.

- [ ] **Sector rotation signals**
  Aggregate DCF results by sector → produce a sector-level BUY/HOLD/SELL score based on the percentage of stocks in each signal bucket. Feed directly into the Confluence Panel.

- [ ] **Earnings calendar integration**
  Flag tickers whose next earnings date is within 2 weeks — useful context before acting on a DCF signal.

### Sentiment Hub

- [ ] **Add AAII Sentiment Survey**
  Weekly retail investor bullish/bearish % — a classic contrarian indicator. Public data from `aaii.com`. Add as a 5th Z-score component.

- [ ] **Add options skew (25-delta skew)**
  Put/call ratio is a volume measure. Skew measures pricing asymmetry — a better real-money fear gauge. Available via broker APIs or CBOE website.

- [ ] **Longer Fear & Greed history**
  CNN only provides ~253 days of history. Backfill from `alternative.me` API or construct a custom composite going back to 2006 using the existing components.

### Macro Hub

- [ ] **Add leading indicators**
  The current PMI proxy (IPMAN) is a lagging industrial production measure. Better leading indicators:
  - ISM Manufacturing PMI (requires ISM data feed or scraping)
  - Conference Board LEI (FRED: USECRILEINDEXM)
  - Credit impulse (change in new credit as % of GDP)

- [ ] **Regime probability instead of hard classification**
  Instead of a binary Goldilocks/Recession label, produce a probability distribution across all four regimes using rolling windows of indicator readings. More nuanced signal for the Confluence Panel.

---

## Phase 4 — Architecture Evolution (3–6 Months)

Goal: Scale the system beyond a single Windows machine without rewriting the core logic.

### Containerisation

- [ ] **Dockerise all ETL scripts**
  Each hub becomes a Docker container with its own `requirements.txt` and entrypoint.
  Enables running on any machine without manual Python environment setup.
  ```
  market-intelligence-suite/
    cot-etl/     Dockerfile + requirements.txt
    liquidity-etl/
    macro-etl/
    dcf-etl/
    sentiment-etl/
    docker-compose.yml
  ```

- [ ] **Replace Windows Task Scheduler with a proper scheduler**
  Options in order of complexity:
  - **Prefect** (Python-native, local agent, free tier) — lowest lift, keeps Python
  - **Apache Airflow** (industry standard, more setup) — if you want DAG visualisation
  - **GitHub Actions** (free, cloud-hosted, cron trigger) — if you move SQL Server to cloud
  
  Any of these gives you: retry on failure, dependency between tasks (Stage 1 → Stage 2 for DCF), run history, and alerting out of the box.

### Cloud SQL Server

- [ ] **Migrate from local SQL Server 2019 to Azure SQL**
  Removes the Personal Gateway requirement for Power BI.
  Power BI Service connects directly to Azure SQL — no machine needs to be on.
  Azure SQL free tier is sufficient for this data volume.

- [ ] **Move SQLite intermediate store to Azure Blob Storage**
  The `sp500_prices.db` file currently lives on the local machine.
  Store it in a Blob container so the DCF pipeline can run from any container.

### Signal Backtesting

- [ ] **Build a backtesting module**
  The most impactful addition once the infrastructure is stable.
  For each instrument: join historical COT Z-scores, Macro regime, Sentiment composite, and Liquidity regime on date → calculate forward returns at 1W, 2W, 4W horizons → measure signal accuracy per confluence score.
  
  This answers the only question that matters: **do the confluence signals actually predict returns?**

---

## Milestone Summary

| Milestone | Target | Definition of Done |
|---|---|---|
| v1.0 — Stable | Week 4 | All Phase 1 fixes applied, 170 tests green, credentials secured |
| v1.1 — Observable | Week 8 | Structured logging, email alerts, data quality checks, 250 tests |
| v1.2 — Enhanced | Week 16 | Incremental COT, Financials DCF fix, AAII + skew in Sentiment, leading indicators in Macro |
| v2.0 — Containerised | Month 4 | Docker, proper scheduler, Azure SQL, no Personal Gateway dependency |
| v2.1 — Backtested | Month 6 | Signal accuracy measured historically for all 12 COT instruments |

---

## Decisions to Make Before Phase 3

These require a choice before implementation begins — they affect design decisions downstream.

1. **Scheduler choice for Phase 4:** Prefect vs. Airflow vs. GitHub Actions. Prefect is the fastest path if you want to stay Python-native. Airflow if you want a team-facing UI. GitHub Actions if you're comfortable moving to cloud SQL first.

2. **Financials DCF model:** Excess return model (theoretically correct) vs. keeping Net Income proxy (simpler, already working). The current P&L signal is still directionally useful even if the absolute values are wrong.

3. **Backtesting scope:** Per-instrument (12 COT markets) or full S&P 500 (503 DCF signals)? Per-instrument is 10× faster to build and immediately actionable for futures trading. S&P 500 requires position sizing logic and benchmark comparison.

4. **Alert delivery:** Email via `smtplib` (simple, works today) vs. Slack webhook (better for mobile) vs. Windows toast notification (local only). Slack webhook is 15 minutes to set up and works anywhere.
