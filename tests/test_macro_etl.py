"""
Tests for Macro_Inflation_Watch/etl (public use).py

Covers:
  - pct_change_yoy     (12-period % change)
  - pct_change_mom     (1-period % change)
  - to_monthly_first   (daily → monthly resampling)
  - classify_regime    (4-regime classification logic)
"""

import numpy as np
import pandas as pd
import pytest
from datetime import date


# ── pct_change_yoy ────────────────────────────────────────────────────────────

class TestPctChangeYoy:
    def test_returns_series(self, macro):
        s = pd.Series(range(1, 25), dtype=float)
        result = macro.pct_change_yoy(s)
        assert isinstance(result, pd.Series)

    def test_first_12_values_are_nan(self, macro):
        s = pd.Series(range(1, 25), dtype=float)
        result = macro.pct_change_yoy(s)
        assert result.iloc[:12].isna().all()

    def test_known_yoy_value(self, macro):
        # 100 → 110 over 12 periods = +10%
        s = pd.Series([100.0] * 12 + [110.0])
        result = macro.pct_change_yoy(s)
        assert result.iloc[-1] == pytest.approx(10.0, rel=1e-3)

    def test_deflation_negative(self, macro):
        s = pd.Series([100.0] * 12 + [90.0])
        result = macro.pct_change_yoy(s)
        assert result.iloc[-1] == pytest.approx(-10.0, rel=1e-3)


# ── pct_change_mom ────────────────────────────────────────────────────────────

class TestPctChangeMom:
    def test_first_value_is_nan(self, macro):
        s = pd.Series([100.0, 101.0, 103.0])
        result = macro.pct_change_mom(s)
        assert pd.isna(result.iloc[0])

    def test_known_mom_value(self, macro):
        s = pd.Series([100.0, 101.0])
        result = macro.pct_change_mom(s)
        assert result.iloc[1] == pytest.approx(1.0, rel=1e-3)

    def test_negative_mom(self, macro):
        s = pd.Series([100.0, 98.0])
        result = macro.pct_change_mom(s)
        assert result.iloc[1] == pytest.approx(-2.0, rel=1e-3)


# ── to_monthly_first ──────────────────────────────────────────────────────────

class TestToMonthlyFirst:
    def test_daily_collapses_to_monthly(self, macro):
        idx = pd.date_range("2024-01-01", "2024-03-31", freq="D")
        df = pd.DataFrame({"value": 1.0}, index=idx)
        result = macro.to_monthly_first(df)
        # Jan, Feb, Mar → 3 rows
        assert len(result) == 3

    def test_output_index_is_month_start(self, macro):
        idx = pd.date_range("2024-01-15", periods=5, freq="D")
        df = pd.DataFrame({"value": range(5)}, index=idx)
        result = macro.to_monthly_first(df)
        assert result.index[0] == pd.Timestamp("2024-01-01")

    def test_mean_is_taken_within_month(self, macro):
        idx = pd.date_range("2024-01-01", periods=2, freq="D")
        df = pd.DataFrame({"value": [10.0, 20.0]}, index=idx)
        result = macro.to_monthly_first(df)
        assert result["value"].iloc[0] == pytest.approx(15.0)


# ── classify_regime ───────────────────────────────────────────────────────────


class TestClassifyRegime:
    def test_goldilocks(self, macro):
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 2.0, "gdp_smoothed": 3.0}))
        assert result["regime_label"] == "Goldilocks"
        assert result["regime_code"] == 1

    def test_inflation(self, macro):
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 4.0, "gdp_smoothed": 3.0}))
        assert result["regime_label"] == "Inflation"
        assert result["regime_code"] == 2

    def test_stagflation(self, macro):
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 5.0, "gdp_smoothed": 1.0}))
        assert result["regime_label"] == "Stagflation"
        assert result["regime_code"] == 3

    def test_recession(self, macro):
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 1.5, "gdp_smoothed": 0.5}))
        assert result["regime_label"] == "Recession"
        assert result["regime_code"] == 4

    def test_nan_cpi_returns_unknown(self, macro):
        result = macro.classify_regime(pd.Series({"cpi_smoothed": float("nan"), "gdp_smoothed": 3.0}))
        assert result["regime_label"] == "Unknown"
        assert result["regime_code"] == 0

    def test_nan_gdp_returns_unknown(self, macro):
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 2.0, "gdp_smoothed": float("nan")}))
        assert result["regime_label"] == "Unknown"
        assert result["regime_code"] == 0

    def test_boundary_cpi_3_gdp_2_is_inflation(self, macro):
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 3.0, "gdp_smoothed": 2.0}))
        assert result["regime_label"] == "Inflation"

    def test_boundary_cpi_2_9_gdp_2_is_goldilocks(self, macro):
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 2.9, "gdp_smoothed": 2.0}))
        assert result["regime_label"] == "Goldilocks"

    def test_boundary_cpi_3_gdp_1_9_is_stagflation(self, macro):
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 3.0, "gdp_smoothed": 1.9}))
        assert result["regime_label"] == "Stagflation"

    def test_boundary_cpi_2_9_gdp_1_9_is_recession(self, macro):
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 2.9, "gdp_smoothed": 1.9}))
        assert result["regime_label"] == "Recession"

    # ── 7 new tests (W4-3) ────────────────────────────────────────────────────

    def test_goldilocks_at_canonical_inputs(self, macro):
        """cpi=2.0, gdp=3.0 — both well inside Goldilocks quadrant."""
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 2.0, "gdp_smoothed": 3.0}))
        assert result["regime_label"] == "Goldilocks"
        assert result["regime_code"] == 1

    def test_inflation_mid_range(self, macro):
        """cpi=4.0, gdp=3.0 — textbook inflation reading."""
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 4.0, "gdp_smoothed": 3.0}))
        assert result["regime_label"] == "Inflation"
        assert result["regime_code"] == 2

    def test_stagflation_mid_range(self, macro):
        """cpi=4.0, gdp=1.0 — high inflation with weak growth."""
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 4.0, "gdp_smoothed": 1.0}))
        assert result["regime_label"] == "Stagflation"
        assert result["regime_code"] == 3

    def test_recession_mid_range(self, macro):
        """cpi=2.0, gdp=1.0 — low inflation, weak growth → Recession."""
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 2.0, "gdp_smoothed": 1.0}))
        assert result["regime_label"] == "Recession"
        assert result["regime_code"] == 4

    def test_both_nan_returns_unknown(self, macro):
        result = macro.classify_regime(pd.Series({"cpi_smoothed": float("nan"), "gdp_smoothed": float("nan")}))
        assert result["regime_label"] == "Unknown"
        assert result["regime_code"] == 0

    def test_boundary_cpi_3_high_gdp_is_inflation(self, macro):
        """CPI exactly at 3.0 with GDP well above threshold → Inflation."""
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 3.0, "gdp_smoothed": 3.0}))
        assert result["regime_label"] == "Inflation"

    def test_boundary_gdp_at_2_low_cpi_is_goldilocks(self, macro):
        """GDP exactly at 2.0 with CPI below threshold → Goldilocks."""
        result = macro.classify_regime(pd.Series({"cpi_smoothed": 2.0, "gdp_smoothed": 2.0}))
        assert result["regime_label"] == "Goldilocks"
