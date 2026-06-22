"""
Tests for DCF_Hub/calculate_dcf (public use).py

Covers:
  - calculate_dcf           (present-value formula)
  - intrinsic_per_share     (total DCF → per-share)
  - valuation_gap           (% gap between intrinsic and market)
  - assign_signal           (BUY / HOLD / SELL thresholds)
  - assign_quality_tier     (sector-aware quality tier logic)
"""

import math
import numpy as np
import pytest


# ── calculate_dcf ─────────────────────────────────────────────────────────────

class TestCalculateDcf:
    def test_returns_positive_float(self, dcf):
        result = dcf.calculate_dcf(1_000_000, 0.10, 0.09)
        assert result is not None
        assert result > 0

    def test_none_fcf_returns_none(self, dcf):
        assert dcf.calculate_dcf(None, 0.10, 0.09) is None

    def test_zero_fcf_returns_none(self, dcf):
        assert dcf.calculate_dcf(0, 0.10, 0.09) is None

    def test_nan_fcf_returns_none(self, dcf):
        assert dcf.calculate_dcf(float("nan"), 0.10, 0.09) is None

    def test_discount_equal_to_terminal_returns_none(self, dcf):
        # discount - terminal == 0 → division by zero guard
        assert dcf.calculate_dcf(1_000_000, 0.05, 0.025, terminal=0.025) is None

    def test_discount_below_terminal_returns_none(self, dcf):
        assert dcf.calculate_dcf(1_000_000, 0.05, 0.02, terminal=0.025) is None

    def test_higher_growth_produces_higher_value(self, dcf):
        low  = dcf.calculate_dcf(1_000_000, 0.05, 0.09)
        high = dcf.calculate_dcf(1_000_000, 0.15, 0.09)
        assert high > low

    def test_higher_discount_produces_lower_value(self, dcf):
        cheap = dcf.calculate_dcf(1_000_000, 0.10, 0.07)
        pricey = dcf.calculate_dcf(1_000_000, 0.10, 0.12)
        assert cheap > pricey

    def test_negative_fcf_produces_negative_value(self, dcf):
        # Loss-making company: FCF < 0
        result = dcf.calculate_dcf(-500_000, 0.10, 0.09)
        assert result < 0

    def test_formula_pv_sum_component(self, dcf):
        # Verify the formula manually for a trivial 1-year case
        # With years=1, terminal_growth=0: total = fcf*(1+g)/(1+d) + fcf*(1+g)/(d-tg) / (1+d)
        # Use the actual function but verify it's in a sensible range
        fcf = 1_000_000
        result = dcf.calculate_dcf(fcf, 0.10, 0.09, terminal=0.025, years=5)
        # 5-year DCF of 1M FCF at 10%/9% should be roughly 15-25x FCF
        assert 10_000_000 < result < 30_000_000


# ── intrinsic_per_share ───────────────────────────────────────────────────────

class TestIntrinsicPerShare:
    def test_basic_calculation(self, dcf):
        # total_value=500M, market_cap=250M, price=$50 → shares=5M → iv=$100
        result = dcf.intrinsic_per_share(500_000_000, 250_000_000, 50.0)
        assert result == pytest.approx(100.0, rel=1e-4)

    def test_none_total_value_returns_none(self, dcf):
        assert dcf.intrinsic_per_share(None, 250_000_000, 50.0) is None

    def test_zero_price_returns_none(self, dcf):
        assert dcf.intrinsic_per_share(500_000_000, 250_000_000, 0) is None

    def test_none_price_returns_none(self, dcf):
        assert dcf.intrinsic_per_share(500_000_000, 250_000_000, None) is None

    def test_zero_market_cap_returns_none(self, dcf):
        assert dcf.intrinsic_per_share(500_000_000, 0, 50.0) is None

    def test_none_market_cap_returns_none(self, dcf):
        assert dcf.intrinsic_per_share(500_000_000, None, 50.0) is None


# ── valuation_gap ─────────────────────────────────────────────────────────────

class TestValuationGap:
    def test_undervalued(self, dcf):
        # Intrinsic $110 vs market $100 → +10%
        result = dcf.valuation_gap(110, 100)
        assert result == pytest.approx(0.10, rel=1e-4)

    def test_overvalued(self, dcf):
        # Intrinsic $90 vs market $100 → -10%
        result = dcf.valuation_gap(90, 100)
        assert result == pytest.approx(-0.10, rel=1e-4)

    def test_none_intrinsic_returns_none(self, dcf):
        assert dcf.valuation_gap(None, 100) is None

    def test_zero_price_returns_none(self, dcf):
        assert dcf.valuation_gap(110, 0) is None

    def test_none_price_returns_none(self, dcf):
        assert dcf.valuation_gap(110, None) is None


# ── assign_signal ─────────────────────────────────────────────────────────────

class TestAssignSignal:
    def test_buy_above_10_pct(self, dcf):
        assert dcf.assign_signal(0.15) == "BUY"

    def test_boundary_exactly_10_pct_is_hold(self, dcf):
        # Threshold is gap > 0.10 (strict), so 0.10 → HOLD
        assert dcf.assign_signal(0.10) == "HOLD"

    def test_hold_within_range(self, dcf):
        assert dcf.assign_signal(0.05) == "HOLD"
        assert dcf.assign_signal(0.0) == "HOLD"
        assert dcf.assign_signal(-0.05) == "HOLD"

    def test_boundary_exactly_neg_10_pct_is_hold(self, dcf):
        # Threshold is gap < -0.10 (strict), so -0.10 → HOLD
        assert dcf.assign_signal(-0.10) == "HOLD"

    def test_sell_below_neg_10_pct(self, dcf):
        assert dcf.assign_signal(-0.15) == "SELL"

    def test_none_gap_returns_insufficient_data(self, dcf):
        assert dcf.assign_signal(None) == "INSUFFICIENT DATA"

    def test_nan_gap_returns_insufficient_data(self, dcf):
        assert dcf.assign_signal(float("nan")) == "INSUFFICIENT DATA"


# ── assign_quality_tier ───────────────────────────────────────────────────────

class TestAssignQualityTier:

    # ── Standard sectors ────────────────────────────────────────────────────

    def test_standard_high_tier(self, dcf):
        # margin > 15%, D/E < 0.5 → High
        assert dcf.assign_quality_tier(0.20, 0.3, "Industrials") == "High"

    def test_standard_high_tier_no_de(self, dcf):
        assert dcf.assign_quality_tier(0.20, None, "Industrials") == "High"

    def test_standard_medium_tier_margin_ok_de_high(self, dcf):
        # margin > 5%, D/E >= 0.5 and < 1.0 → Medium
        assert dcf.assign_quality_tier(0.10, 0.7, "Materials") == "Medium"

    def test_standard_low_tier_de_too_high(self, dcf):
        # margin > 5% but D/E >= 1.0 → Low
        assert dcf.assign_quality_tier(0.10, 1.5, "Energy") == "Low"

    def test_standard_low_tier_thin_margin(self, dcf):
        assert dcf.assign_quality_tier(0.03, 0.2, "Industrials") == "Low"

    # ── Financials ──────────────────────────────────────────────────────────

    def test_financials_high_tier(self, dcf):
        # margin > 15%, D/E < 2.0 → High (looser D/E threshold)
        assert dcf.assign_quality_tier(0.20, 1.5, "Financials") == "High"

    def test_financials_medium_tier(self, dcf):
        assert dcf.assign_quality_tier(0.10, 3.0, "Financials") == "Medium"

    def test_financials_low_tier(self, dcf):
        assert dcf.assign_quality_tier(0.02, 1.0, "Financials") == "Low"

    # ── Utilities ───────────────────────────────────────────────────────────

    def test_utilities_high_tier(self, dcf):
        # margin > 10%, D/E < 1.5 → High
        assert dcf.assign_quality_tier(0.12, 1.2, "Utilities") == "High"

    def test_utilities_medium_tier(self, dcf):
        assert dcf.assign_quality_tier(0.07, 2.0, "Utilities") == "Medium"

    # ── Real Estate ─────────────────────────────────────────────────────────

    def test_real_estate_high_tier(self, dcf):
        # margin > 10% → High (D/E not checked for Real Estate)
        assert dcf.assign_quality_tier(0.12, 5.0, "Real Estate") == "High"

    def test_real_estate_medium_tier(self, dcf):
        assert dcf.assign_quality_tier(0.05, 5.0, "Real Estate") == "Medium"

    def test_real_estate_low_tier(self, dcf):
        assert dcf.assign_quality_tier(0.01, 5.0, "Real Estate") == "Low"

    # ── Edge cases ──────────────────────────────────────────────────────────

    def test_none_margin_returns_low(self, dcf):
        assert dcf.assign_quality_tier(None, 0.3, "Industrials") == "Low"

    def test_nan_margin_returns_low(self, dcf):
        assert dcf.assign_quality_tier(float("nan"), 0.3, "Industrials") == "Low"
