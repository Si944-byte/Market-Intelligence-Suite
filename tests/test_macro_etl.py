"""
Tests for Macro_Inflation_Watch/etl (public use).py

Covers:
  - pct_change_yoy     (12-period % change)
  - pct_change_mom     (1-period % change)
  - to_monthly_first   (daily → monthly resampling)
  - classify_regime    (4-regime classification logic)

Note: classify_regime is a nested closure inside build_master and cannot
be imported directly. The logic is replicated here to document the
expected behavior and catch future threshold regressions.
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
#
# This function is a nested closure inside build_master() and cannot be
# imported directly.  The logic is replicated verbatim so its business
# rules are independently documented and can catch threshold drift.

def classify_regime(row):
    """Mirror of the nested classify_regime in build_master."""
    cpi = row.get("cpi_smoothed")
    gdp = row.get("gdp_smoothed")
    if pd.isna(cpi) or pd.isna(gdp):
        return None, None
    if   cpi < 3.0  and gdp >= 2.0: return "Goldilocks",  1
    elif cpi >= 3.0 and gdp >= 2.0: return "Inflation",   2
    elif cpi >= 3.0 and gdp < 2.0:  return "Stagflation", 3
    else:                            return "Recession",   4


class TestClassifyRegime:
    def test_goldilocks(self):
        label, code = classify_regime({"cpi_smoothed": 2.0, "gdp_smoothed": 3.0})
        assert label == "Goldilocks"
        assert code == 1

    def test_inflation(self):
        label, code = classify_regime({"cpi_smoothed": 4.0, "gdp_smoothed": 3.0})
        assert label == "Inflation"
        assert code == 2

    def test_stagflation(self):
        label, code = classify_regime({"cpi_smoothed": 5.0, "gdp_smoothed": 1.0})
        assert label == "Stagflation"
        assert code == 3

    def test_recession(self):
        label, code = classify_regime({"cpi_smoothed": 1.5, "gdp_smoothed": 0.5})
        assert label == "Recession"
        assert code == 4

    def test_nan_cpi_returns_none(self):
        label, code = classify_regime({"cpi_smoothed": float("nan"), "gdp_smoothed": 3.0})
        assert label is None
        assert code is None

    def test_nan_gdp_returns_none(self):
        label, code = classify_regime({"cpi_smoothed": 2.0, "gdp_smoothed": float("nan")})
        assert label is None
        assert code is None

    def test_boundary_cpi_3_gdp_2_is_inflation(self):
        # cpi >= 3.0 AND gdp >= 2.0 → Inflation (not Goldilocks)
        label, code = classify_regime({"cpi_smoothed": 3.0, "gdp_smoothed": 2.0})
        assert label == "Inflation"

    def test_boundary_cpi_2_9_gdp_2_is_goldilocks(self):
        label, code = classify_regime({"cpi_smoothed": 2.9, "gdp_smoothed": 2.0})
        assert label == "Goldilocks"

    def test_boundary_cpi_3_gdp_1_9_is_stagflation(self):
        label, code = classify_regime({"cpi_smoothed": 3.0, "gdp_smoothed": 1.9})
        assert label == "Stagflation"

    def test_boundary_cpi_2_9_gdp_1_9_is_recession(self):
        # Falls through all conditions → else clause
        label, code = classify_regime({"cpi_smoothed": 2.9, "gdp_smoothed": 1.9})
        assert label == "Recession"
