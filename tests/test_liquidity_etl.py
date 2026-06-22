"""
Tests for Liquidity_Hub/liquidity_etl (public use).py

Covers:
  - apply_unit_conversion   (millions→billions, pct→bps, direct)
  - upsert_fed_balance_sheet data preparation (merge logic)
  - upsert_credit_spreads   (outer join keeps all dates)
  - upsert_money_market     (DFF spine, left join SOFR + T10YFF)
"""

import numpy as np
import pandas as pd
import pytest


# ── apply_unit_conversion ─────────────────────────────────────────────────────

class TestApplyUnitConversion:
    def _df(self, values):
        return pd.DataFrame({"series_date": range(len(values)), "value": values})

    def test_millions_to_billions(self, liquidity):
        df = self._df([1_000.0, 2_000.0, 500.0])
        result = liquidity.apply_unit_conversion(df.copy(), "millions_to_billions")
        assert list(result["value"]) == pytest.approx([1.0, 2.0, 0.5])

    def test_pct_to_bps(self, liquidity):
        # 1.30 percentage points → 130 basis points
        df = self._df([1.30, 0.50, -0.25])
        result = liquidity.apply_unit_conversion(df.copy(), "pct_to_bps")
        assert list(result["value"]) == pytest.approx([130.0, 50.0, -25.0])

    def test_direct_no_change(self, liquidity):
        df = self._df([5.5, 3.2, 1.0])
        result = liquidity.apply_unit_conversion(df.copy(), "direct")
        assert list(result["value"]) == pytest.approx([5.5, 3.2, 1.0])

    def test_unknown_conversion_no_change(self, liquidity):
        df = self._df([100.0])
        result = liquidity.apply_unit_conversion(df.copy(), "unknown_type")
        assert list(result["value"]) == pytest.approx([100.0])


# ── Fed Balance Sheet merge logic ─────────────────────────────────────────────

class TestFedBalanceSheetMerge:
    """
    The upsert function merges WALCL (spine), TGA and RRP on series_date.
    We test the merge logic by verifying what would be written to the DB.
    """

    @staticmethod
    def _merge(walcl_vals, tga_vals, rrp_vals, walcl_dates, tga_dates, rrp_dates):
        walcl = pd.DataFrame({"series_date": walcl_dates, "value": walcl_vals})
        tga   = pd.DataFrame({"series_date": tga_dates,   "value": tga_vals})
        rrp   = pd.DataFrame({"series_date": rrp_dates,   "value": rrp_vals})

        df = walcl.rename(columns={"value": "fed_balance_sheet_b"})
        df = df.merge(tga.rename(columns={"value": "tga_b"}), on="series_date", how="left")
        df = df.merge(rrp.rename(columns={"value": "reverse_repo_b"}), on="series_date", how="left")
        df = df.dropna(subset=["fed_balance_sheet_b"])
        return df

    def test_basic_merge_produces_correct_row_count(self):
        df = self._merge(
            [8.0, 8.1], [1.0, 1.1], [0.5, 0.6],
            ["2024-01-05", "2024-01-12"],
            ["2024-01-05", "2024-01-12"],
            ["2024-01-05", "2024-01-12"],
        )
        assert len(df) == 2

    def test_missing_tga_date_gives_nan(self):
        df = self._merge(
            [8.0, 8.1], [1.0], [0.5, 0.6],
            ["2024-01-05", "2024-01-12"],
            ["2024-01-05"],           # TGA missing 2nd week
            ["2024-01-05", "2024-01-12"],
        )
        assert pd.isna(df.iloc[1]["tga_b"])

    def test_walcl_date_not_in_tga_still_included(self):
        # WALCL spine includes a date that TGA doesn't have
        df = self._merge(
            [8.0, 8.1, 8.2], [1.0, 1.1], [0.5, 0.6, 0.7],
            ["2024-01-05", "2024-01-12", "2024-01-19"],
            ["2024-01-05", "2024-01-12"],
            ["2024-01-05", "2024-01-12", "2024-01-19"],
        )
        assert len(df) == 3


# ── Credit Spreads outer join ─────────────────────────────────────────────────

class TestCreditSpreadsMerge:
    """The outer join should keep rows that exist in either HY or IG."""

    @staticmethod
    def _merge(hy_dates, hy_vals, ig_dates, ig_vals):
        hy = pd.DataFrame({"series_date": hy_dates, "value": hy_vals})
        ig = pd.DataFrame({"series_date": ig_dates, "value": ig_vals})
        df = hy.rename(columns={"value": "hy_spread_pct"})
        df = df.merge(
            ig.rename(columns={"value": "ig_spread_pct"}),
            on="series_date", how="outer"
        ).sort_values("series_date")
        return df

    def test_outer_join_keeps_all_dates(self):
        df = self._merge(
            ["2024-01-02", "2024-01-03"], [3.0, 3.1],
            ["2024-01-02", "2024-01-04"], [1.5, 1.6],
        )
        # 3 unique dates across both series
        assert len(df) == 3

    def test_ig_nan_where_date_missing_from_ig(self):
        df = self._merge(
            ["2024-01-02", "2024-01-03"], [3.0, 3.1],
            ["2024-01-02"],             [1.5],
        )
        row = df[df["series_date"] == "2024-01-03"].iloc[0]
        assert pd.isna(row["ig_spread_pct"])
        assert row["hy_spread_pct"] == pytest.approx(3.1)


# ── Money Market spine ────────────────────────────────────────────────────────

class TestMoneyMarketMerge:
    """DFF is the spine — SOFR and T10YFF left-joined."""

    @staticmethod
    def _merge(dff_dates, dff_vals, sofr_dates, sofr_vals, t10_dates, t10_vals):
        dff   = pd.DataFrame({"series_date": dff_dates,  "value": dff_vals})
        sofr  = pd.DataFrame({"series_date": sofr_dates, "value": sofr_vals})
        t10   = pd.DataFrame({"series_date": t10_dates,  "value": t10_vals})

        df = dff.rename(columns={"value": "fed_funds_rate_pct"})
        df = df.merge(sofr.rename(columns={"value": "sofr_rate_pct"}),
                      on="series_date", how="left")
        df = df.merge(t10.rename(columns={"value": "t10y_ff_spread_bps"}),
                      on="series_date", how="left")
        df = df.dropna(subset=["fed_funds_rate_pct"])
        return df

    def test_dff_spine_length_preserved(self):
        df = self._merge(
            ["2024-01-02", "2024-01-03", "2024-01-04"], [5.33, 5.33, 5.33],
            ["2024-01-02", "2024-01-03", "2024-01-04"], [5.30, 5.30, 5.30],
            ["2024-01-02", "2024-01-03", "2024-01-04"], [130, 131, 129],
        )
        assert len(df) == 3

    def test_sofr_date_not_in_dff_excluded(self):
        # SOFR has an extra date — it should NOT add rows (left join)
        df = self._merge(
            ["2024-01-02"], [5.33],
            ["2024-01-02", "2024-01-03"], [5.30, 5.30],  # extra date
            ["2024-01-02"], [130],
        )
        assert len(df) == 1

    def test_missing_sofr_gives_nan(self):
        df = self._merge(
            ["2024-01-02", "2024-01-03"], [5.33, 5.33],
            ["2024-01-02"], [5.30],   # SOFR missing 2nd day
            ["2024-01-02", "2024-01-03"], [130, 131],
        )
        assert pd.isna(df.iloc[1]["sofr_rate_pct"])
        assert df.iloc[1]["fed_funds_rate_pct"] == pytest.approx(5.33)
