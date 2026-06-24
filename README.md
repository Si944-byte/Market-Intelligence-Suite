# Market Intelligence Suite

![Python](https://img.shields.io/badge/Python-3.x-blue?logo=python)
![SQL Server](https://img.shields.io/badge/SQL%20Server-2019-red?logo=microsoft-sql-server)
![Power BI](https://img.shields.io/badge/Power%20BI-Desktop%2FService-yellow?logo=powerbi)
![FRED](https://img.shields.io/badge/Data-FRED%20API-green)
![CFTC](https://img.shields.io/badge/Data-CFTC.gov-blue)
![Tests](https://img.shields.io/badge/Tests-177%20passing-brightgreen)
![Status](https://img.shields.io/badge/Status-Live%20%26%20Automated-brightgreen)

A personal institutional-grade market intelligence system built for
systematic futures trading. Six interconnected dashboards covering
liquidity conditions, macro regime, DCF valuation, market sentiment,
COT institutional positioning, and a multi-instrument backtesting engine
— all feeding into a unified weekly pre-trade decision framework.

Built entirely from scratch: Python ETL pipelines, SQL Server 2019,
Power BI Desktop/Service, and Windows Task Scheduler automation.
No third-party BI templates or pre-built connectors.

---

## Dashboards

### 1. Liquidity Regime Dashboard
**Database:** LiquidityRegime | **Refresh:** Saturday 8:00 PM (ETL) | 9:00 PM (Power BI)

<img width="1160" height="811" alt="Screenshot 2026-04-29 140534" src="https://github.com/user-attachments/assets/e99d59f4-660b-4c6d-a2b8-c7a734114062" />

Tracks systemic liquidity conditions via Fed balance sheet decomposition,
credit spreads, and yield curve. Produces a 0–100 composite gauge and
Expanding / Neutral / Contracting regime classification.

**Data source:** FRED (all series, daily + weekly cadence)

| FRED Series | Metric | Cadence |
|-------------|--------|---------|
| WALCL | Fed Balance Sheet | Weekly Thu |
| WTREGEN | Treasury TGA | Weekly Thu |
| WLRRAL | Reverse Repo (RRP) | Weekly Thu |
| BAMLH0A0HYM2 | HY Credit Spread | Daily |
| BAMLC0A0CM | IG Credit Spread | Daily |
| DFF | Fed Funds Rate | Daily |
| SOFR | SOFR Rate | Daily |
| T10YFF | 10Y minus Fed Funds | Daily |

**Key formulas:**
- Net Liquidity = Fed Balance Sheet − TGA − RRP
- HY Z-Score = (Current HY Spread − 2yr Mean) / 2yr StdDev
- Composite Score = (Net Liq × 0.50) + (HY Z-Score × 0.30) + (Yield Curve × 0.20)
- Gauge = ((Score + 2) / 4) × 100 → Range: 0–100

**Regime scoring:**

| Regime | Gauge | Composite | Trade Bias |
|--------|-------|-----------|------------|
| Expanding | 69–100 | > 0.75 | Risk-On — Lean Long |
| Neutral-Positive | 50–69 | > 0.00 | Mild Risk-On Bias |
| Neutral | 37–50 | > -0.50 | No Liquidity Edge |
| Neutral-Negative | 25–37 | > -1.00 | Caution — Reduce Size |
| Contracting | 0–25 | ≤ -1.00 | Risk-Off — ZN/ZB |

**SQL architecture:**
- Staging: stg_FedBalanceSheet (weekly), stg_CreditSpreads (daily), stg_MoneyMarket (daily), DimDate (static)
- Views: vw_NetLiquidity, vw_CreditSpreads, vw_MoneyMarket, vw_LiquidityRegime, vw_RegimeHistory

**5 pages:** Liquidity Regime Dashboard · Net Liquidity Deep Dive ·
Credit & Money Markets · Trading Confluence Panel · Methodology

---

### 2. Macro Regime Dashboard
**Database:** MacroRegime | **Refresh:** Sunday 5:00 AM (ETL) | 6:00 AM (Power BI)

<img width="1359" height="765" alt="Screenshot 2026-04-29 144726" src="https://github.com/user-attachments/assets/f908b0be-5e20-477a-895d-f8016106631b" />

Tracks the macroeconomic environment across four regime states:
Expansion, Slowdown, Contraction, Recovery.

- **Data sources:** FRED (CPI, unemployment, FFR), RapidAPI YFinance (S&P 500), IPMAN as PMI proxy
- **Key metrics:** 6-month GDP smoothing, 3-month CPI smoothing, composite macro score with +1/0/−1 signal system
- **SQL views:** All Power BI transforms done in SQL — no DAX heavy lifting
- **Output:** Market bias score, regime classification, multi-page Power BI dashboard

---

### 3. DCF Valuation Dashboard
**Database:** DCFRegime | **Refresh:** Sunday 5:00 AM (ETL) | 6:00 AM (Power BI)

<img width="1443" height="812" alt="Screenshot 2026-04-29 143031" src="https://github.com/user-attachments/assets/5e5b76c3-323d-4808-8456-3e7884543080" />

Covers all 503 S&P 500 stocks via RapidAPI Yahoo Finance.

- **Framework:** Three Pillars — Valuation & Research Engine, Stress-Testing Engine
- **Key features:** Durability classification, outlier flagging, Sensitivity Score per stock, Gap Buckets reference table
- **Key finding:** 48.5% of BUY signals fail conservative stress testing. Financials sector flagged for Phase 2 P/B/ROE treatment
- **Output:** Stock-level valuation gap, buy/hold/sell signals, stress test pass/fail, multi-page Power BI dashboard

---

### 4. Market Sentiment Dashboard
**Database:** SentimentRegime | **Refresh:** Saturday 5:30 AM (ETL) | 6:30 AM (Power BI)

<img width="1440" height="812" alt="Screenshot 2026-04-29 144821" src="https://github.com/user-attachments/assets/9a7b888e-6d3b-4101-bae1-ea780fc36884" />

Tracks market crowd psychology across multiple sentiment indicators.

- **Data sources:** CBOE (put/call ratio), CNN Fear & Greed
- **Key metrics:** Composite sentiment score, regime classification, historical percentile context
- **Output:** Sentiment regime label, composite score trending, Power BI dashboard with historical context

---

### 5. COT Positioning Dashboard
**Database:** COTRegime | **Refresh:** Friday 6:00 PM (ETL) | 7:00 PM (Power BI)

<img width="1282" height="722" alt="Screenshot 2026-04-29 140924" src="https://github.com/user-attachments/assets/f7943f2b-bf74-439d-9620-7d6b7bb98b6b" />

Tracks CFTC Commitment of Traders institutional positioning for 12
futures markets across Legacy and Disaggregated report formats.

- **Instruments:** ES, NQ, YM, ZN, ZB, 6E, CL, GC, SI, ZC, ZS, NG
- **Key metrics:** 52-week rolling Z-scores for spec and commercial positioning, Bullish/Bearish Divergence signals, Divergence Strength score
- **ETL:** Downloads ~20 CFTC annual ZIPs per cycle, validates null rates, rebuilds 10,000+ row fact table
- **Output:** Positioning heatmap, divergence confluence panel, historical extremes, five-page Power BI dashboard

See `docs/COT_Dashboard_Guide.docx` for full COT methodology.

---

### 6. Backtest Hub
**Database:** BacktestRegime | **Refresh:** Sunday 5:30 AM (ETL) | 7:00 AM (Power BI)

<img width="1279" height="722" alt="Screenshot 2026-06-23 210359" src="https://github.com/user-attachments/assets/4b709770-456e-4fc1-8a08-3fec75f4c036" />

| Property | Detail |
|---|---|
| Scripts | `Backtest_Hub/backtest_databento_etl (public use).py` (Step 1 — Databento price pull) |
| | `Backtest_Hub/backtest_news_etl (public use).py` (Step 2) |
| | `Backtest_Hub/backtest_signals (public use).py` (Step 3) |
| | `Backtest_Hub/backtest_regime_tag (public use).py` (Step 4) |
| | `Backtest_Hub/backtest_simulate (public use).py` (Step 5) |
| | `Backtest_Hub/backtest_price_etl (public use).py` (utility — initial CSV import only) |
| Database | BacktestRegime |
| Schedule | Sunday 5:30 AM ETL → Sunday 7:00 AM Power BI refresh |
| Data source | Databento GLBX.MDP3 API (price) · ForexFactory (news events) |
| Instruments | 13 micro futures (MES, MNQ, MYM, MGC, SIL, MCL, 6E, ZN, ZB, ZC, ZS, CL, NG) |
| History | Jan 2021 – present (4H/1H) · limited 5M history |

**What it does:** Pulls 5 years of price history from Databento (GLBX.MDP3, 1M OHLCV resampled to 5M/1H/4H), generates
Multi-Level Break of Structure (BoS) signals across 13 instruments and three
timeframes (4H bias → 1H BoS → 5M entry), tags each signal with the prevailing
Macro/Liquidity/COT/Sentiment regime, runs a four-tier risk simulation, and
produces win rate and R-multiple statistics by instrument, direction, and
Confluence Score.

**Key findings (Cycle 1 — 3,778,718 bars · 4,724 signals · 17,932 trade rows):**
- Score 3 confluence: 46% win rate, 1.12 avg R (vs. 24–25% at Score 0–1)
- 6E LONG and ZN LONG excluded from trading plan (2.7% and 12.7% win rates)
- Primary instruments: ZC LONG, MNQ LONG, MGC SHORT

---

## How It Works Together

Each dashboard feeds a layer of the pre-trade decision framework:

```
Liquidity        → Is the tide rising or falling? (Fed BS, TGA, RRP, spreads)
        |
Macro Regime     → What is the macroeconomic environment?
        |
COT Positioning  → What are institutions positioned to do?
        |
Sentiment        → What is the crowd's emotional state?
        |
DCF Valuation    → Are equity prices justified? (position sizing)
        |
Break of Structure setup → Entry trigger (TradingView, 4H/1H/5M)
```

The Trading Confluence Panel on the Liquidity Dashboard pulls signals
from all four other dashboards into a single per-instrument bias table,
producing an Overall Confluence score and Confluence Regime Display.

**Signal hierarchy:**
- Liquidity expanding + Macro expansion = maximum risk-on conviction
- Any contraction signal = reduce size regardless of other signals
- All four dashboards aligned = full position size
- Mixed signals = minimum size or stand aside

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| ETL | Python 3 (pandas, requests, pyodbc, sqlalchemy, fredapi) |
| Database | SQL Server 2019 (local instance) |
| Visualization | Power BI Desktop + Power BI Service |
| Automation | Windows Task Scheduler + Personal Gateway |
| Data Sources | FRED API, RapidAPI (Yahoo Finance), CFTC.gov, CBOE, Databento GLBX.MDP3 |
| Testing | pytest (170 tests) |

---

## Automation Schedule

| Dashboard | ETL | Power BI Refresh |
|-----------|-----|-----------------|
| COT Positioning | Friday 6:00 PM | Friday 7:00 PM |
| Liquidity | Saturday 8:00 PM | Saturday 9:00 PM |
| Sentiment | Saturday 5:30 AM | Saturday 6:30 AM |
| Macro Regime | Sunday 5:00 AM | Sunday 6:00 AM |
| DCF Valuation | Sunday 5:00 AM | Sunday 6:00 AM |
| Backtest | Sunday 5:30 AM | Sunday 7:00 AM |

All automation runs via Windows Task Scheduler calling batch files,
with Power BI Service scheduled refresh via Personal Gateway
connecting to local SQL Server.

---

## Repository Structure

```
market-intelligence-suite/
├── Backtest_Hub/            # Backtest ETL (price, news, signals, regime, simulate)
├── COT_Hub/                 # COT positioning ETL
├── DCF_Hub/                 # DCF valuation ETL (calculate + fetch_fundamentals)
├── Liquidity_Hub/           # Liquidity regime ETL
├── Macro_Inflation_Watch/   # Macro regime ETL
├── Sentiment_Hub/           # Sentiment ETL
├── tests/                   # pytest suite — 170 tests across all 6 ETL modules
├── docs/                    # Architecture, data flow, and roadmap docs
├── requirements.txt         # Pinned production dependencies
├── requirements-test.txt    # pytest + test dependencies
├── FIX_GUIDE.md             # Week 1 & 2 stabilization fix log
└── TECHNICAL_DEBT.md        # Known issues and future work
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

Create a `.env` file in the project root (never commit this):

```env
SQL_SERVER=YOUR_SQL_SERVER
SQL_USER=YOUR_SQL_USER
SQL_PASSWORD=YOUR_SQL_PASSWORD
FRED_API_KEY=YOUR_FRED_API_KEY
RAPIDAPI_KEY=YOUR_RAPIDAPI_KEY
DCF_DB_PATH=C:\path\to\sp500_prices.db
DCF_TICKERS_PATH=C:\path\to\sp500_tickers.csv
DCF_OUTPUT_PATH=C:\path\to\Stock_Data_Current.csv
DATABENTO_API_KEY=YOUR_DATABENTO_API_KEY
DATABENTO_BACKUP_FOLDER=C:\path\to\Data\Databento
```

All six ETL scripts load credentials automatically via `python-dotenv` — no hardcoded values.

### 3. Run tests

```bash
python -m pytest tests/ -v
```

All 170 tests should pass before running any ETL against a live database.

---

## ETL Reliability Features

All six ETL scripts share a common set of production hardening patterns applied during the stabilization pass:

| Feature | Detail |
|---------|--------|
| Credentials via `.env` | No secrets in source code; `python-dotenv` loads at startup |
| Transaction safety | TRUNCATE + INSERT wrapped in `BEGIN TRANSACTION` / `ROLLBACK` — partial writes are impossible |
| Atomic DELETE + append | SQLAlchemy `engine.begin()` ensures DELETE and `to_sql` share one transaction |
| Exponential backoff retry | External API calls retry up to 3 times: 5s → 10s → 20s waits |
| Connection context manager | `managed_conn()` auto-commits on success, rolls back on exception, always closes |
| Leading-zero CFTC codes | `pd.read_csv(..., dtype=str)` preserves codes like `043602` (ZN) that integer parsing would drop |

---

*Built for personal systematic trading research.
Not financial advice.*
