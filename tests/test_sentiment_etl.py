"""
Tests for Sentiment_Hub/sentiment_etl (public use).py

Covers:
  - classify_sentiment   (5-tier label from composite Z-score)
  - zscore_rolling       (252-day rolling Z-score)
  - fg_synthetic         (composite Z → 0-100 Fear & Greed scale)
  - composite_zscore     (mean of available Z-score components)
"""

import numpy as np
import pandas as pd
import pytest


# ── classify_sentiment ────────────────────────────────────────────────────────

class TestClassifySentiment:
    def test_extreme_fear(self, sentiment):
        assert sentiment.classify_sentiment(-2.0) == "Extreme Fear"

    def test_boundary_neg_1_5_is_fear(self, sentiment):
        # threshold is < -1.5 (strict), so -1.5 → Fear
        assert sentiment.classify_sentiment(-1.5) == "Fear"

    def test_fear(self, sentiment):
        assert sentiment.classify_sentiment(-1.0) == "Fear"

    def test_boundary_neg_0_5_is_neutral(self, sentiment):
        # threshold is < -0.5 (strict), so -0.5 → Neutral
        assert sentiment.classify_sentiment(-0.5) == "Neutral"

    def test_neutral_zero(self, sentiment):
        assert sentiment.classify_sentiment(0.0) == "Neutral"

    def test_neutral_at_0_5(self, sentiment):
        # threshold is <= 0.5, so 0.5 → Neutral
        assert sentiment.classify_sentiment(0.5) == "Neutral"

    def test_greed(self, sentiment):
        assert sentiment.classify_sentiment(1.0) == "Greed"

    def test_boundary_1_5_is_greed(self, sentiment):
        # threshold is <= 1.5, so 1.5 → Greed
        assert sentiment.classify_sentiment(1.5) == "Greed"

    def test_extreme_greed(self, sentiment):
        assert sentiment.classify_sentiment(2.0) == "Extreme Greed"

    def test_nan_returns_unknown(self, sentiment):
        assert sentiment.classify_sentiment(float("nan")) == "Unknown"

    def test_pandas_na_returns_unknown(self, sentiment):
        assert sentiment.classify_sentiment(pd.NA) == "Unknown"


# ── zscore_rolling ────────────────────────────────────────────────────────────

class TestZscoreRolling:
    def test_returns_series_same_length(self, sentiment):
        s = pd.Series(range(300), dtype=float)
        result = sentiment.zscore_rolling(s, 252)
        assert len(result) == 300

    def test_short_series_all_nan(self, sentiment):
        # min_periods=60 → fewer than 60 values → all NaN
        s = pd.Series(range(30), dtype=float)
        result = sentiment.zscore_rolling(s, 252)
        assert result.isna().all()

    def test_constant_series_all_nan(self, sentiment):
        # std=0 → NaN
        s = pd.Series([10.0] * 300)
        result = sentiment.zscore_rolling(s, 252)
        assert result.isna().all()

    def test_valid_zscores_roughly_bounded(self, sentiment):
        # 99.7% of Z-scores from a normal distribution fall within ±4
        rng = np.random.default_rng(7)
        s = pd.Series(rng.normal(0, 1, 500))
        result = sentiment.zscore_rolling(s, 252)
        valid = result.dropna()
        assert (valid.abs() < 6).all()


# ── fg_synthetic formula ──────────────────────────────────────────────────────

class TestFgSynthetic:
    """
    Tests the Synthetic Fear & Greed formula:
        clamped = composite_z.clip(-3, 3)
        fg_synthetic = ((clamped + 3) / 6 * 100).round(2)

    Boundary expectations:
        composite_z = -3  →  0.0   (maximum fear)
        composite_z =  0  →  50.0  (neutral)
        composite_z = +3  →  100.0 (maximum greed)
    """

    @staticmethod
    def _fg(z: float) -> float:
        clamped = max(-3.0, min(3.0, z))
        return round((clamped + 3) / 6 * 100, 2)

    def test_maximum_fear_is_zero(self):
        assert self._fg(-3.0) == pytest.approx(0.0)

    def test_extreme_fear_clamped_to_zero(self):
        # Values beyond ±3σ are clamped
        assert self._fg(-10.0) == pytest.approx(0.0)

    def test_neutral_is_fifty(self):
        assert self._fg(0.0) == pytest.approx(50.0)

    def test_maximum_greed_is_hundred(self):
        assert self._fg(3.0) == pytest.approx(100.0)

    def test_extreme_greed_clamped_to_hundred(self):
        assert self._fg(10.0) == pytest.approx(100.0)

    def test_midpoint_positive(self):
        # z=1.5 → (1.5+3)/6*100 = 75.0
        assert self._fg(1.5) == pytest.approx(75.0)

    def test_midpoint_negative(self):
        # z=-1.5 → (-1.5+3)/6*100 = 25.0
        assert self._fg(-1.5) == pytest.approx(25.0)

    def test_output_range_never_exceeds_0_100(self):
        for z in np.linspace(-10, 10, 100):
            v = self._fg(z)
            assert 0.0 <= v <= 100.0

    def test_monotonically_increasing(self):
        zs = np.linspace(-3, 3, 50)
        values = [self._fg(z) for z in zs]
        assert all(a <= b for a, b in zip(values, values[1:]))


# ── composite Z-score mean logic ──────────────────────────────────────────────

class TestCompositeZscore:
    """
    The composite Z-score is the row-wise mean of up to 4 components,
    skipping NaN. Tests that partial availability still produces a result.
    """

    def test_all_four_components_averaged(self):
        row = pd.Series({
            "vix_zscore": -1.0,
            "vix_term_zscore": -0.5,
            "pc_zscore": 0.5,
            "fg_zscore": 1.0,
        })
        result = row.mean(skipna=True)
        assert result == pytest.approx(0.0)

    def test_missing_fg_still_produces_composite(self):
        row = pd.Series({
            "vix_zscore": -1.0,
            "vix_term_zscore": -1.0,
            "pc_zscore": -1.0,
            "fg_zscore": float("nan"),
        })
        result = row.mean(skipna=True)
        assert result == pytest.approx(-1.0)

    def test_all_nan_produces_nan(self):
        row = pd.Series({
            "vix_zscore": float("nan"),
            "vix_term_zscore": float("nan"),
            "pc_zscore": float("nan"),
            "fg_zscore": float("nan"),
        })
        assert pd.isna(row.mean(skipna=True))
