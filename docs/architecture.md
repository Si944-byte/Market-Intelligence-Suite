# Architecture — Market Intelligence Suite

## Overview

The Market Intelligence Suite is a systematic, data-driven pre-trade decision framework built for futures trading. Five independent intelligence hubs each run a weekly ETL pipeline, writing to isolated SQL Server databases that feed Power BI dashboards. All five dashboards feed a unified Trading Confluence Panel that produces a per-instrument bias score before each trading week.

```
External Data Sources
        │
        ▼
Python ETL Pipelines  (Windows Task Scheduler)
        │
        ▼
SQL Server 2019  (5 isolated databases)
        │
        ▼
Power BI Service  (Personal Gateway refresh)
        │
        ▼
Trading Confluence Panel  →  TradingView Entry
```

For a rendered visual diagram open `docs/architecture.html` in any browser.

---

## Hubs

### 1. COT Positioning Hub

| Property | Detail |
|---|---|
| Script | `COT_Hub/cot_etl - public use.py` |
| Database | COTRegime |
| Schedule | Friday 6:00 PM ETL → Friday 7:00 PM Power BI refresh |
| Data source | CFTC.gov public ZIPs (no API key required) |
| Instruments | 12 futures markets |

**What it does:** Downloads every annual CFTC Commitment of Traders report from 2006 to present (~20 ZIPs per run), parses two report formats (Legacy and Disaggregated), calculates net positioning and 52-week rolling Z-scores for speculative and commercial traders, and assigns Bullish/Bearish Divergence signals when positions reach extremes.

**Instruments covered:**

| Symbol | Name | Report Format | Group |
|---|---|---|---|
| ES | E-Mini S&P 500 | Legacy | Equity Index |
| NQ | Nasdaq-100 Mini | Legacy | Equity Index |
| YM | DJIA Mini | Legacy | Equity Index |
| ZN | 10-Year T-Note | Legacy | Rates |
| ZB | 30-Year T-Bond | Legacy | Rates |
| 6E | Euro FX | Legacy | FX |
| CL | Crude Oil | Disaggregated | Energy |
| GC | Gold | Disaggregated | Metals |
| SI | Silver | Disaggregated | Metals |
| ZC | Corn | Disaggregated | Ags |
| ZS | Soybeans | Disaggregated | Ags |
| NG | Natural Gas | Disaggregated | Energy |

**SQL Schema — COTRegime:**

```
raw_cot (staging)
  report_date · cftc_code · symbol · instrument_name · report_type
  nc_long · nc_short · comm_long · comm_short · nonrept_long · nonrept_short
  mm_long · mm_short · prod_long · prod_short · swap_long · swap_short
  other_long · other_short · open_interest
  PK: (report_date, cftc_code)

cot_weekly (fact — ~10,000 rows, rebuilt weekly)
  All staging columns plus:
  net_noncomm · net_comm · net_nonrept          (Legacy net positions)
  net_managed_money · net_producer · net_swap   (Disagg net positions)
  noncomm_pct_oi · comm_pct_oi                  (% of open interest)
  mm_pct_oi · prod_pct_oi
  noncomm_zscore · comm_zscore                  (52-week rolling Z)
  mm_zscore · prod_zscore
  primary_zscore · commercial_zscore            (computed columns)
  net_position_primary
  positioning_label                              (5-tier: Extreme Short → Extreme Long)
  PK: (report_date, cftc_code)

vw_cot_latest      — most recent row per instrument (KPI cards)
vw_cot_history     — full history + 13-week smoothed primary Z (trend chart)
vw_cot_extremes    — top 5 most extreme long/short per instrument
```

**Signal logic:**
- Positioning label: 5 tiers based on primary spec Z-score (±0.5 Neutral, ±1.5 Extreme)
- Bullish Divergence: Spec Z < −1.0 AND Commercial Z > +1.0
- Bearish Divergence: Spec Z > +1.0 AND Commercial Z < −1.0
- Divergence Strength: |Spec Z| + |Commercial Z|

---

### 2. Liquidity Hub

| Property | Detail |
|---|---|
| Script | `Liquidity_Hub/liquidity_etl (public use).py` |
| Database | LiquidityRegime |
| Schedule | Saturday 8:00 PM ETL → Saturday 9:00 PM Power BI refresh |
| Data source | FRED API (8 series) |

**What it does:** Tracks systemic liquidity conditions via Federal Reserve balance sheet decomposition. Calculates Net Liquidity (Fed BS − TGA − RRP), monitors credit spread regimes, and produces a 0-100 composite gauge with an Expanding/Neutral/Contracting classification.

**Core formula:** `Net Liquidity = Fed Balance Sheet − Treasury General Account − Reverse Repo`

**SQL Schema — LiquidityRegime:**

```
stg_FedBalanceSheet (staging — weekly)
  series_date · fed_balance_sheet_b · tga_b · reverse_repo_b · loaded_at

stg_CreditSpreads (staging — daily)
  series_date · hy_spread_pct · ig_spread_pct · loaded_at

stg_MoneyMarket (staging — daily)
  series_date · sofr_rate_pct · fed_funds_rate_pct · t10y_ff_spread_bps · loaded_at

stg_SPX (staging — daily)
  series_date · spx_close · loaded_at

DimDate (12,419 rows — 2002-01-01 through 2035-12-31)
  date_key · full_date · year_num · quarter_num · month_num
  month_name · month_short · week_num · day_of_week · day_name
  is_weekday · year_month · fiscal_year · quarter_label

Views (all transforms done in SQL — no DAX heavy lifting):
  vw_NetLiquidity       — net liquidity calculation + trend
  vw_CreditSpreads      — HY/IG spread levels and regimes
  vw_MoneyMarket        — SOFR/FFR/yield curve
  vw_LiquidityRegime    — composite score + gauge value + trade bias
  vw_RegimeHistory      — historical regime classifications
```

---

### 3. Macro Regime Hub

| Property | Detail |
|---|---|
| Script | `Macro_Inflation_Watch/etl (public use).py` |
| Database | MacroRegime |
| Schedule | Sunday 5:00 AM ETL → Sunday 6:00 AM Power BI refresh |
| Data sources | FRED API (11 series) + RapidAPI Yahoo Finance (^GSPC) |

**What it does:** Classifies the macroeconomic environment into one of four regimes using smoothed CPI and GDP data. Produces a composite macro score signal (+1/0/−1) per instrument based on regime conditions.

**Regime classification:**

| Regime | CPI (smoothed) | GDP (smoothed) | Code |
|---|---|---|---|
| Goldilocks | < 3.0% | ≥ 2.0% | 1 |
| Inflation | ≥ 3.0% | ≥ 2.0% | 2 |
| Stagflation | ≥ 3.0% | < 2.0% | 3 |
| Recession | < 3.0% | < 2.0% | 4 |

Smoothing: 3-month rolling mean on CPI YoY%, 6-month rolling mean on GDP.

**SQL Schema — MacroRegime:**

```
Staging tables (rebuilt each run via DELETE + append):
  raw_cpi          — 6 CPI series (headline, core, housing, food, energy, transport)
  raw_ffr          — Fed Funds Rate
  raw_unemployment — Unemployment Rate
  raw_gdp          — Real GDP Growth QoQ Annualised
  raw_yield_curve  — 10Y-2Y Treasury Spread
  raw_pmi          — Industrial Production Manufacturing Index (PMI proxy)
  raw_spx          — S&P 500 monthly closes

macro_monthly (fact — 2010-present, ~175 rows)
  date
  cpi_level · cpi_core_level · cpi_mom_pct · cpi_yoy_pct · cpi_core_yoy_pct
  cpi_smoothed · cpi_housing · cpi_food · cpi_energy · cpi_transport
  ffr · real_interest_rate
  unemployment_rate
  gdp_real_growth · gdp_smoothed
  yield_spread_10y2y · yield_curve_inverted
  spx_close · spx_return_1m · spx_return_12m
  pmi · pmi_expanding
  regime_label · regime_code
```

---

### 4. DCF Valuation Hub

| Property | Detail |
|---|---|
| Scripts | `DCF_Hub/fetch_fundamentals_rapidapi (public use).py` (Stage 1) `DCF_Hub/calculate_dcf (public use).py` (Stage 2) |
| Databases | SQLite `sp500_prices.db` (intermediate) → DCFRegime (SQL Server) |
| Schedule | Sunday 5:00 AM ETL → Sunday 6:00 AM Power BI refresh |
| Data source | RapidAPI Yahoo Finance (2 calls per ticker × 503 tickers) |

**What it does:** Fetches fundamental data for all S&P 500 constituents, runs a three-scenario DCF valuation (base, conservative, aggressive), assigns quality tiers based on sector-adjusted margin and leverage thresholds, and produces BUY/HOLD/SELL signals with stress-test overlays.

**DCF Assumptions:**

| Scenario | Growth | Discount |
|---|---|---|
| Conservative | 5% | 10% |
| Base (sector-specific) | 5%–12% | 7%–9% |
| Aggressive | 15% | 7% |
| Terminal growth (all) | 2.5% | — |
| Projection period | 5 years | — |

**Sector base-case assumptions:**

| Sector | Growth | Discount |
|---|---|---|
| Information Technology | 12% | 9% |
| Communication Services | 10% | 9% |
| Consumer Discretionary | 9% | 9% |
| Health Care | 9% | 8% |
| Industrials | 8% | 8% |
| Financials | 8% | 8% |
| Materials | 7% | 8% |
| Energy | 7% | 8% |
| Real Estate | 6% | 7% |
| Consumer Staples | 6% | 7% |
| Utilities | 5% | 7% |

**Quality tier thresholds:**

| Sector | High | Medium | Low |
|---|---|---|---|
| Standard | Margin > 15% AND D/E < 0.5 | Margin > 5% AND D/E < 1.0 | Otherwise |
| Financials | Margin > 15% AND D/E < 2.0 | Margin > 5% | Otherwise |
| Utilities | Margin > 10% AND D/E < 1.5 | Margin > 5% | Otherwise |
| Real Estate | Margin > 10% | Margin > 3% | Otherwise |

**SQL Schema — DCFRegime:**

```
dbo.dcf_results (fact)
  Data_Date · Ticker · Company · Sector
  Current_Price · Intrinsic_Value_Per_Share · Intrinsic_Value_Total
  Valuation_Gap_Pct · Valuation_Gap_Dollars
  Market_Cap · FCF · FCF_Yield_Pct · Revenue · Total_Debt
  Debt_to_Equity · Operating_Cash_Flow · Capital_Expenditure
  Profit_Margin · Operating_Margin · Week52_Low · Week52_High
  Quality_Tier · Signal · DCF_Method
  Sector_Growth_Rate · Sector_Discount_Rate
  Conservative_IV · Conservative_Gap
  Aggressive_IV · Aggressive_Gap
  UNIQUE: (Data_Date, Ticker)
```

**Signal logic:**
- BUY: valuation gap > +10%
- HOLD: valuation gap ±10%
- SELL: valuation gap < −10%
- Stress test: Conservative gap must also be positive to be flagged "Robust BUY"

**SQLite intermediate store (`sp500_prices.db`):**
```
fundamentals
  id · fetch_date · ticker · company · sector
  current_price · market_cap · revenue · fcf
  operating_cash_flow · capital_expenditure · net_income
  total_debt · debt_to_equity · profit_margin · operating_margin
  week52_high · week52_low · dcf_method · created_at
  UNIQUE: (fetch_date, ticker)  ← enables resume on partial runs
```

---

### 5. Sentiment Hub

| Property | Detail |
|---|---|
| Script | `Sentiment_Hub/sentiment_etl (public use).py` |
| Database | SentimentRegime |
| Schedule | Saturday 5:30 AM ETL → Saturday 6:30 AM Power BI refresh |
| Data sources | FRED API (VIX daily + 9-day) · CBOE archive CSV + HTML scrape · CNN Fear & Greed endpoint |

**What it does:** Builds a composite market sentiment score from four independent signals, normalises each to a Z-score over a 252-trading-day (1-year) rolling window, combines them into a single composite, and maps that composite to a synthetic 0-100 Fear & Greed scale for Power BI visualisation.

**Signal construction:**

| Component | Source | Direction | Window |
|---|---|---|---|
| VIX Z-score | FRED VIXCLS | Inverted (high VIX = fear = negative) | 252 days |
| VIX term ratio Z-score | FRED VXVCLS / VIXCLS | Inverted | 252 days |
| Put/Call ratio Z-score | CBOE equity P/C | Inverted (high P/C = fear = negative) | 252 days |
| Fear & Greed Z-score | CNN dataviz endpoint | Direct | 252 days |

**Synthetic F&G formula:**
```
composite_z  = mean(vix_z, term_z, pc_z, fg_z)   [skipna=True]
clamped      = composite_z.clip(-3, +3)
fg_synthetic = ((clamped + 3) / 6 * 100).round(2)
```
Maps: −3σ → 0 (maximum fear), 0 → 50 (neutral), +3σ → 100 (maximum greed).

**Sentiment label tiers:**

| Range | Label |
|---|---|
| Z < −1.5 | Extreme Fear |
| −1.5 ≤ Z < −0.5 | Fear |
| −0.5 ≤ Z ≤ +0.5 | Neutral |
| +0.5 < Z ≤ +1.5 | Greed |
| Z > +1.5 | Extreme Greed |

**SQL Schema — SentimentRegime:**

```
raw_vix          — date · vix_close · vix9d_close
raw_putcall      — date · equity_pc_ratio
raw_fear_greed   — date · fg_score  (real CNN data, ~253 days)

sentiment_daily (fact — 2006-present, ~5,000 rows)
  date
  vix_close · vix9d_close · vix_term_ratio
  equity_pc_ratio
  fg_score        (real CNN data where available)
  fg_synthetic    (calculated, full history back to 2006)
  vix_zscore · vix_term_zscore · pc_zscore · fg_zscore
  composite_zscore · sentiment_label
  PK: date
```

---

## Infrastructure

### Execution Layer

```
Windows Task Scheduler
  ├── Friday  6:00 PM  →  run_cot_etl.bat        →  cot_etl.py
  ├── Saturday 5:30 AM →  run_sentiment_etl.bat   →  sentiment_etl.py
  ├── Saturday 8:00 PM →  run_liquidity_etl.bat   →  liquidity_etl.py
  ├── Sunday  5:00 AM  →  run_etl.bat             →  etl.py (Macro)
  └── Sunday  5:00 AM  →  run_dcf.bat             →  fetch_fundamentals.py → calculate_dcf.py
```

### Data Layer

- **SQL Server 2019** (local instance)
- 5 isolated databases — one per hub
- All Power BI transforms done in SQL views (no DAX calculated columns)
- DimDate in LiquidityRegime covers 2002–2035 (12,419 rows)

### Visualisation Layer

- **Power BI Desktop** connected to local SQL Server
- **Power BI Service** via Personal Gateway
- Refresh triggered 1 hour after each ETL completes
- All dashboards published to the same Power BI workspace

### Signal Hierarchy (Pre-Trade Decision Framework)

```
1. Macro Regime          ← defines the environment (highest weight)
2. COT Positioning       ← confirms institutional bias
3. Sentiment             ← crowd psychology overlay
4. DCF Valuation         ← equity position sizing
5. Liquidity             ← market tide (risk-on / risk-off)
────────────────────────
6. TradingView           ← Break of Structure entry (4H / 1H / 5M)
```

**Confluence rules:**
- All 5 dashboards aligned → full position size
- Mixed signals → minimum size or stand aside
- Any Liquidity Contraction signal → reduce size regardless of other signals

---

## Repository Structure

```
Market-Intelligence-Suite/
├── COT_Hub/
│   ├── cot_etl - public use.py
│   └── run_cot_etl.bat
├── DCF_Hub/
│   ├── fetch_fundamentals_rapidapi (public use).py
│   ├── calculate_dcf (public use).py
│   └── run_dcf (public use).bat
├── Liquidity_Hub/
│   ├── liquidity_etl (public use).py
│   └── run_liquidity_etl.bat
├── Macro_Inflation_Watch/
│   ├── etl (public use).py
│   └── run_etl.bat
├── Sentiment_Hub/
│   ├── sentiment_etl (public use).py
│   └── run_sentiment_etl.bat
├── tests/
│   ├── conftest.py
│   ├── test_cot_etl.py
│   ├── test_calculate_dcf.py
│   ├── test_sentiment_etl.py
│   ├── test_liquidity_etl.py
│   ├── test_macro_etl.py
│   └── test_fetch_fundamentals.py
├── docs/
│   ├── architecture.md          ← this file
│   ├── architecture.html        ← rendered Mermaid diagram
│   ├── data_flow.md
│   └── roadmap.md
├── TECHNICAL_DEBT.md
├── FIX_GUIDE.md
├── requirements-test.txt
└── changelog.md
```
