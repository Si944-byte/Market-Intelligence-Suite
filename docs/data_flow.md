# Data Flow — Market Intelligence Suite

This document traces every data point from its external source through the ETL pipeline, into SQL Server, and out to Power BI. Organised by hub, with column-level detail for the most important transforms.

---

## COT Positioning Hub

### Sources

| Source | URL Pattern | Format | Frequency |
|---|---|---|---|
| CFTC Legacy | `cftc.gov/files/dea/history/deacot{YEAR}.zip` | ZIP → CSV `annual.txt` | Annual (one file per year) |
| CFTC Disaggregated | `cftc.gov/files/dea/history/fut_disagg_txt_{YEAR}.zip` | ZIP → CSV | Annual |

- Downloads ~20 ZIPs per run (2006 → current year), both formats
- No API key required — all public CFTC files
- CFTC changed e-mini equity index codes in May 2023: pre-May uses `13874A` (ES), post-May uses `13874+` (consolidated). The ETL maintains a reverse lookup and resolves all codes back to the base instrument.

### Transform Chain

```
CFTC ZIP download (per year, per format)
    │
    ▼
pd.read_csv() → strip column whitespace → filter to 12 target codes
    │
    ▼
Date parse: YYMMDD (6-digit) or YYYYMMDD (8-digit) → Python date
    │
    ▼
Column map → standardised field names (nc_long, mm_short, etc.)
    │
    ▼
Consolidated ID resolution (13874+ → 13874A)
    │
    ▼
pd.concat all years → drop_duplicates(report_date, cftc_code, keep=last)
    │
    ▼
TRUNCATE + INSERT → raw_cot  (~10,000 rows)
    │
    ▼
Per-instrument groupby:
  net_noncomm     = nc_long - nc_short
  net_comm        = comm_long - comm_short
  noncomm_pct_oi  = net_noncomm / open_interest * 100
  noncomm_zscore  = rolling(52wk, min=10).zscore(net_noncomm)
  comm_zscore     = rolling(52wk, min=10).zscore(net_comm)
  positioning_label = classify_positioning(primary_zscore)
    │
    ▼
TRUNCATE + INSERT → cot_weekly
    │
    ▼
UPDATE computed columns:
  primary_zscore      = COALESCE(noncomm_zscore, mm_zscore)
  commercial_zscore   = COALESCE(comm_zscore, prod_zscore)
  net_position_primary = COALESCE(net_noncomm, net_managed_money)
    │
    ▼
CREATE/REPLACE views → vw_cot_latest, vw_cot_history, vw_cot_extremes
```

### Key Derived Fields

| Field | Formula | Notes |
|---|---|---|
| `net_noncomm` | `nc_long − nc_short` | Large speculator net position |
| `net_comm` | `comm_long − comm_short` | Commercial hedger net position |
| `noncomm_pct_oi` | `net_noncomm / open_interest × 100` | Normalised for OI-adjusted comparison |
| `noncomm_zscore` | 52-week rolling Z of `net_noncomm` | min_periods=10 |
| `positioning_label` | ±0.5 Neutral, ±1.5 Extreme tiers | Based on primary spec Z |
| `primary_zscore` | `COALESCE(noncomm_zscore, mm_zscore)` | Legacy → noncomm; Disagg → managed money |
| Bullish Divergence | Spec Z < −1.0 AND Comm Z > +1.0 | Crowd short, commercials buying |
| Bearish Divergence | Spec Z > +1.0 AND Comm Z < −1.0 | Crowd long, commercials selling |

### Output to Power BI

```
vw_cot_latest    →  KPI cards (current positioning per instrument)
vw_cot_history   →  Z-score trend chart (includes 13-week smoothed primary Z)
vw_cot_extremes  →  Top 5 historical extremes (long and short sides)
```

---

## Liquidity Hub

### Sources

| FRED Series | Description | Frequency | Units | Conversion |
|---|---|---|---|---|
| WALCL | Fed Total Assets (Balance Sheet) | Weekly | $M | ÷ 1,000 → $B |
| WTREGEN | Treasury General Account | Weekly | $M | ÷ 1,000 → $B |
| WLRRAL | ON Reverse Repo | Weekly | $M | ÷ 1,000 → $B |
| BAMLH0A0HYM2 | HY OAS Credit Spread | Daily | % | Direct |
| BAMLC0A0CM | IG OAS Credit Spread | Daily | % | Direct |
| SOFR | Secured Overnight Financing Rate | Daily | % | Direct |
| DFF | Effective Fed Funds Rate | Daily | % | Direct |
| T10YFF | 10Y Treasury minus Fed Funds | Daily | % pts | × 100 → bps |
| SP500 | S&P 500 Index | Daily | Index | Direct |

History start: 2002-01-01. FRED missing-value placeholder `.` is filtered out at parse time.

### Transform Chain

```
FRED API → JSON observations → filter out "." missing values → pd.DataFrame
    │
    ▼
apply_unit_conversion()  (millions→billions  |  pct→bps  |  direct)
    │
    ▼
Fed Balance Sheet:
  WALCL as date spine
  LEFT JOIN WTREGEN on series_date
  LEFT JOIN WLRRAL on series_date
  DROP rows where WALCL is null
    │
    ▼
MERGE → stg_FedBalanceSheet (one row per weekly date)

Credit Spreads:
  HY as left frame
  OUTER JOIN IG on series_date (keeps dates from either source)
    │
    ▼
MERGE → stg_CreditSpreads (one row per trading day)

Money Market:
  DFF as date spine (most complete daily series)
  LEFT JOIN SOFR
  LEFT JOIN T10YFF (already converted to bps)
  DROP rows where DFF is null
    │
    ▼
MERGE → stg_MoneyMarket

DimDate: generated once (2002-01-01 → 2035-12-31), skipped if already populated
    │
    ▼
SQL Views compute all regime logic — no Power BI DAX transforms:
  Net Liquidity = fed_balance_sheet_b − tga_b − reverse_repo_b
  Composite score → 0-100 gauge → Expanding / Neutral / Contracting
```

### Key Derived Fields (computed in SQL Views)

| Field | Formula |
|---|---|
| Net Liquidity | `fed_balance_sheet_b − tga_b − reverse_repo_b` |
| Liquidity trend | Week-over-week delta of net liquidity |
| HY-IG spread | Credit spread differential (risk appetite proxy) |
| Real yield spread | T10YFF − inflation expectations |
| Regime | Composite threshold logic across all three staging tables |
| Gauge Value | 0-100 normalised composite |
| Trade Bias | Risk-On / Neutral / Risk-Off |

### Output to Power BI

```
vw_NetLiquidity     →  Fed balance sheet chart + net liquidity trend
vw_CreditSpreads    →  HY/IG spread levels + spread regime
vw_MoneyMarket      →  SOFR, FFR, yield curve charts
vw_LiquidityRegime  →  Composite gauge + trade bias KPI card
vw_RegimeHistory    →  Historical regime timeline
```

---

## Macro Regime Hub

### Sources

| FRED Series | Description | Table | Transform |
|---|---|---|---|
| CPIAUCSL | CPI Headline | raw_cpi | YoY%, MoM%, 3-month smooth |
| CPILFESL | CPI Core (ex food & energy) | raw_cpi | YoY% |
| CPIHOSSL | CPI Housing | raw_cpi | YoY% |
| CPIFABSL | CPI Food & Beverages | raw_cpi | YoY% |
| CPIENGSL | CPI Energy | raw_cpi | YoY% |
| CPITRNSL | CPI Transportation | raw_cpi | YoY% |
| FEDFUNDS | Fed Funds Rate | raw_ffr | Direct |
| UNRATE | Unemployment Rate | raw_unemployment | Direct |
| A191RL1Q225SBEA | Real GDP QoQ Annualised | raw_gdp | 6-month smooth, forward-filled |
| T10Y2Y | 10Y−2Y Treasury Spread | raw_yield_curve | Inversion flag |
| IPMAN | Industrial Production Manufacturing | raw_pmi | Expansion flag (≥ 100) |
| ^GSPC | S&P 500 | raw_spx | RapidAPI; MoM%, 12M% |

All FRED data is resampled to monthly-first frequency (`resample("MS").mean()`). Start date: 2010-01-01.

### Transform Chain

```
fredapi.get_series() → pd.DataFrame (3 attempts, 30s flat wait between)
yfinance / RapidAPI → S&P 500 monthly closes
    │
    ▼
to_monthly_first(): resample("MS").mean() → align all series to 1st of month
    │
    ▼
Load staging tables (DELETE entire table → to_sql append):
  raw_cpi        (6 series stacked with series_id column)
  raw_ffr
  raw_unemployment
  raw_gdp
  raw_yield_curve
  raw_pmi
  raw_spx
    │
    ▼
build_master():
  date spine = pd.date_range(2010-01-01, today, freq="MS")
  LEFT JOIN all staging tables onto spine
  GDP: quarterly series → forward-filled to monthly
    │
    ▼
Derived columns:
  cpi_mom_pct          = pct_change(1) × 100
  cpi_yoy_pct          = pct_change(12) × 100
  cpi_core_yoy_pct     = pct_change(12) × 100 on core
  real_interest_rate   = ffr − cpi_yoy_pct
  yield_curve_inverted = (yield_spread_10y2y < 0).astype(int)
  spx_return_1m        = pct_change(1) × 100
  spx_return_12m       = pct_change(12) × 100
  pmi_expanding        = (pmi >= 100).astype(int)
  gdp_smoothed         = rolling(6, min_periods=1).mean()
  cpi_smoothed         = rolling(3, min_periods=1).mean()  on cpi_yoy_pct
    │
    ▼
classify_regime(cpi_smoothed, gdp_smoothed):
  Goldilocks  (1): CPI < 3.0%  AND GDP ≥ 2.0%
  Inflation   (2): CPI ≥ 3.0%  AND GDP ≥ 2.0%
  Stagflation (3): CPI ≥ 3.0%  AND GDP < 2.0%
  Recession   (4): CPI < 3.0%  AND GDP < 2.0%
    │
    ▼
DELETE macro_monthly → to_sql append
```

### Output to Power BI

```
macro_monthly  →  All charts and KPI cards
                  Regime label, regime code, all component series
                  Real interest rate, yield curve, SPX returns
```

---

## DCF Valuation Hub

### Stage 1 — Fundamentals Fetch

```
sp500_tickers.csv  (503 tickers with Company + Sector)
    │
    ▼
get_already_fetched(): check SQLite for today's already-fetched tickers
    │
    ▼
For each remaining ticker (2 API calls):

  Call 1: get-financial-data
    quoteSummary → result[0] → financialData
    Extracts: currentPrice, totalRevenue, totalDebt, operatingCashflow,
              freeCashflow, profitMargins, operatingMargins,
              debtToEquity, netIncomeToCommon, ebitda
    D/E conversion: if value > 20 → divide by 100 (API returns %)

  0.3s sleep

  Call 2: get-price
    quoteSummary → result[0] → price
    Extracts: fiftyTwoWeekHigh, fiftyTwoWeekLow, marketCap

  FCF determination:
    Financials sector → use Net Income (structural margin difference)
    All others        → freeCashflow, fallback to operatingCashflow

  0.75s sleep (rate limiting)
    │
    ▼
INSERT OR IGNORE → SQLite fundamentals (UNIQUE on fetch_date, ticker)
```

### Stage 2 — DCF Calculation

```
SQLite fundamentals  (latest fetch_date snapshot)
    │
    ▼
For each ticker:

  Base Case DCF:
    (growth, discount) = SECTOR_ASSUMPTIONS.get(sector, (0.08, 0.08))
    pv_sum = Σ [ fcf × (1+g)^t / (1+d)^t ]  for t = 1..5
    terminal_value = fcf × (1+g)^5 × (1+tg) / (d − tg)
    pv_terminal = terminal_value / (1+d)^5
    base_dcf_total = pv_sum + pv_terminal

  intrinsic_per_share = base_dcf_total / (market_cap / current_price)
  valuation_gap       = (intrinsic_per_share − current_price) / current_price

  Conservative: (growth=0.05, discount=0.10)  → same formula
  Aggressive:   (growth=0.15, discount=0.07)  → same formula

  quality_tier = assign_quality_tier(profit_margin, debt_to_equity, sector)
  signal       = BUY if gap > 10%  |  SELL if gap < -10%  |  HOLD otherwise

  fcf_yield    = fcf / market_cap
  gap_dollars  = (intrinsic_per_share − price) × share_count
    │
    ▼
INSERT WHERE NOT EXISTS → dbo.dcf_results  (UNIQUE on Data_Date, Ticker)
df.to_csv(OUTPUT_PATH)  (CSV backup)
```

### Output to Power BI

```
dbo.dcf_results  →  BUY/HOLD/SELL signal table
                     Valuation gap waterfall chart
                     Sector-level average gap bar chart
                     Stress test pass/fail overlay
                     Quality tier distribution
```

---

## Sentiment Hub

### Sources

| Source | Method | Coverage | Notes |
|---|---|---|---|
| FRED VIXCLS | API | 2006-present | VIX daily close |
| FRED VXVCLS | API | 2006-present | VIX 9-day close |
| CBOE Archive CSV | HTTP GET | 2006-11-01 → 2019-10-04 | Equity put/call ratio |
| CBOE Daily Stats | HTML scrape (pd.read_html) | 2019-10-07 → present | Same ratio, different source |
| CNN Fear & Greed | JSON endpoint | ~253 days (rolling) | Graceful fail — optional component |

### Transform Chain

```
FRED API → JSON observations → skip "." values → date-indexed Series
    │
    ▼
CBOE Archive CSV:
  Find header row starting with "DATE"
  Parse date: MM/DD/YYYY → errors="coerce" → drop NaT
  Parse pc_ratio: to_numeric → drop nulls → filter > 0

CBOE Daily Stats HTML:
  pd.read_html() → find table with "date" AND "p/c" column headers
  Same date/ratio parsing
  pd.concat([archive, current]) → drop_duplicates(date, keep=last)

CNN Fear & Greed:
  GET dataviz endpoint → fear_and_greed_historical.data
  Timestamp (ms) → date.fromtimestamp(ts/1000)
  Return None on any failure (graceful — F&G is optional)
    │
    ▼
UPSERT raw_vix (IF EXISTS UPDATE ELSE INSERT, per date)
UPSERT raw_putcall
UPSERT raw_fear_greed  (skipped if CNN fetch failed)
    │
    ▼
build_sentiment_master():
  Read all three staging tables from SQL Server
  Union of all dates as index
  LEFT JOIN each series onto the date index
  Force all numeric columns to float64

  vix_term_ratio = vix9d_close / vix_close

  Z-score (252-day rolling, min_periods=60):
    vix_zscore      = zscore(vix_close)  × −1      (inverted)
    vix_term_zscore = zscore(vix_term_ratio) × −1  (inverted)
    pc_zscore       = zscore(equity_pc_ratio) × −1 (inverted)
    fg_zscore       = zscore(fg_score)              (direct)

  composite_zscore = row-wise mean(vix_z, term_z, pc_z, fg_z)  skipna=True
  fg_synthetic     = ((composite_z.clip(-3,3) + 3) / 6 × 100).round(2)
  sentiment_label  = classify_sentiment(composite_zscore)
    │
    ▼
UPSERT sentiment_daily  (IF EXISTS UPDATE ELSE INSERT, per date)
```

### Sign Convention

All fear indicators are inverted so that positive composite Z = greed, negative = fear:

| Signal | Raw direction | Z direction |
|---|---|---|
| High VIX | = high fear | × −1 → negative |
| High VIX term ratio (VIX9d/VIX) | = backwardation = near-term fear | × −1 → negative |
| High put/call ratio | = hedging demand = bearish | × −1 → negative |
| High F&G score | = greed | unchanged → positive |

### Output to Power BI

```
sentiment_daily  →  Composite Z-score trend line
                     fg_synthetic gauge (0-100)
                     fg_score (real CNN data, last ~253 days)
                     Sentiment label KPI card
                     Component Z-score waterfall
```

---

## Cross-Hub: Trading Confluence Panel

The Liquidity Dashboard hosts a Confluence Panel that joins the latest signal from each hub into a single per-instrument bias table.

### Inputs

| Hub | Signal pulled | Field |
|---|---|---|
| COT | positioning_label per instrument | vw_cot_latest |
| Sentiment | sentiment_label | sentiment_daily (latest) |
| Macro | regime_label | macro_monthly (latest) |
| DCF | Signal (BUY/HOLD/SELL) per ticker | dbo.dcf_results (latest date) |
| Liquidity | trade_bias | vw_LiquidityRegime (latest) |

### Scoring

```
Each hub produces +1 (bullish) / 0 (neutral) / -1 (bearish) per instrument.
Overall Confluence Score = sum of 5 signals  (-5 to +5)

Position sizing:
  Score ≥ +3   →  Full position (aligned bullish)
  Score ≤ -3   →  Full short / avoid (aligned bearish)
  -2 to +2     →  Minimum size or stand aside
  Any Liquidity Contraction  →  Reduce regardless of score
```

---

## Data Freshness Summary

| Hub | Data lag | Latest available |
|---|---|---|
| COT | CFTC publishes Friday ~3:30 PM ET | Friday COT report |
| Liquidity | FRED weekly data lags ~1 week | Prior week's Fed balance sheet |
| Sentiment (VIX) | T+0 via FRED | Previous trading day |
| Sentiment (P/C) | T+0 via CBOE | Previous trading day |
| Sentiment (F&G) | T+0 via CNN | Previous trading day |
| Macro (CPI/FFR) | Lags 1-4 weeks post-release | Previous month |
| Macro (GDP) | Lags ~30 days post-quarter | Prior quarter |
| DCF | T+0 via RapidAPI | Previous trading day's price |
