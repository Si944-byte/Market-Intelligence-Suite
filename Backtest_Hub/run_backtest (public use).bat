@echo off
echo ============================================================
echo  BacktestRegime ETL - Market Intelligence Suite
echo ============================================================

echo [1/5] Databento Price ETL...
python "backtest_databento_etl (public use).py"
if %errorlevel% neq 0 (
    echo ERROR: Databento ETL failed. Aborting.
    exit /b 1
)

echo [2/5] News ETL...
python "backtest_news_etl (public use).py"
if %errorlevel% neq 0 (
    echo ERROR: News ETL failed. Aborting.
    exit /b 1
)

echo [3/5] Signal Generator...
python "backtest_signals (public use).py"
if %errorlevel% neq 0 (
    echo ERROR: Signal generator failed. Aborting.
    exit /b 1
)

echo [4/5] Regime Tagger...
python "backtest_regime_tag (public use).py"
if %errorlevel% neq 0 (
    echo ERROR: Regime tagger failed. Aborting.
    exit /b 1
)

echo [5/5] Simulator...
python "backtest_simulate (public use).py"
if %errorlevel% neq 0 (
    echo ERROR: Simulator failed. Aborting.
    exit /b 1
)

echo ============================================================
echo  BacktestRegime ETL complete.
echo ============================================================
