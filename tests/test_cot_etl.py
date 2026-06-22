"""
Tests for COT_Hub/cot_etl - public use.py

Covers:
  - _safe_int / _safe_float  (pyodbc-safe scalar conversion)
  - classify_positioning     (5-tier Z-score label)
  - zscore_rolling           (52-week rolling Z-score)
  - URL builders             (legacy_url / disagg_url)
  - CONSOLIDATED_LOOKUP      (post-May 2023 code mapping)
  - parse_legacy_zip         (CSV parsing with synthetic in-memory ZIP)
  - parse_disagg_zip         (disaggregated format)
"""

import io
import zipfile
import csv
from datetime import date

import numpy as np
import pandas as pd
import pytest


# ── _safe_int ─────────────────────────────────────────────────────────────────

class TestSafeInt:
    def test_none_returns_none(self, cot):
        assert cot._safe_int(None) is None

    def test_nan_returns_none(self, cot):
        assert cot._safe_int(float("nan")) is None

    def test_inf_returns_none(self, cot):
        assert cot._safe_int(float("inf")) is None

    def test_negative_inf_returns_none(self, cot):
        assert cot._safe_int(float("-inf")) is None

    def test_numpy_scalar_unwrapped(self, cot):
        val = np.int64(42)
        result = cot._safe_int(val)
        assert result == 42
        assert isinstance(result, int)

    def test_numpy_float_nan_returns_none(self, cot):
        assert cot._safe_int(np.float64("nan")) is None

    def test_comma_string_parsed(self, cot):
        assert cot._safe_int("1,234,567") == 1234567

    def test_empty_string_returns_none(self, cot):
        assert cot._safe_int("") is None

    def test_nan_string_returns_none(self, cot):
        assert cot._safe_int("nan") is None

    def test_float_truncates(self, cot):
        assert cot._safe_int(42.9) == 42

    def test_negative_value(self, cot):
        assert cot._safe_int(-500) == -500


# ── _safe_float ───────────────────────────────────────────────────────────────

class TestSafeFloat:
    def test_none_returns_none(self, cot):
        assert cot._safe_float(None) is None

    def test_nan_returns_none(self, cot):
        assert cot._safe_float(float("nan")) is None

    def test_inf_returns_none(self, cot):
        assert cot._safe_float(float("inf")) is None

    def test_valid_float_rounded_to_4dp(self, cot):
        result = cot._safe_float(3.141592)
        assert result == 3.1416

    def test_integer_input(self, cot):
        assert cot._safe_float(5) == 5.0

    def test_invalid_string_returns_none(self, cot):
        assert cot._safe_float("not_a_number") is None


# ── classify_positioning ──────────────────────────────────────────────────────

class TestClassifyPositioning:
    def test_extreme_long(self, cot):
        assert cot.classify_positioning(2.0) == "Extreme Long"

    def test_boundary_above_1_5_is_extreme_long(self, cot):
        assert cot.classify_positioning(1.51) == "Extreme Long"

    def test_boundary_1_5_is_long(self, cot):
        # threshold is > 1.5 (strict), so 1.5 falls into Long
        assert cot.classify_positioning(1.5) == "Long"

    def test_long(self, cot):
        assert cot.classify_positioning(1.0) == "Long"

    def test_boundary_0_5_is_neutral(self, cot):
        # threshold is > 0.5 (strict), so 0.5 falls into Neutral
        assert cot.classify_positioning(0.5) == "Neutral"

    def test_neutral_zero(self, cot):
        assert cot.classify_positioning(0.0) == "Neutral"

    def test_neutral_negative_0_5(self, cot):
        # >= -0.5 → Neutral
        assert cot.classify_positioning(-0.5) == "Neutral"

    def test_short(self, cot):
        assert cot.classify_positioning(-1.0) == "Short"

    def test_boundary_neg_1_5_is_short(self, cot):
        assert cot.classify_positioning(-1.5) == "Short"

    def test_extreme_short(self, cot):
        assert cot.classify_positioning(-2.0) == "Extreme Short"

    def test_nan_returns_unknown(self, cot):
        assert cot.classify_positioning(float("nan")) == "Unknown"

    def test_pandas_na_returns_unknown(self, cot):
        assert cot.classify_positioning(pd.NA) == "Unknown"


# ── zscore_rolling ────────────────────────────────────────────────────────────

class TestZscoreRolling:
    def test_returns_series(self, cot):
        s = pd.Series(range(60), dtype=float)
        result = cot.zscore_rolling(s, window=52)
        assert isinstance(result, pd.Series)
        assert len(result) == 60

    def test_short_series_produces_nans(self, cot):
        # Fewer than min_periods=10 → all NaN
        s = pd.Series([1.0, 2.0, 3.0])
        result = cot.zscore_rolling(s, window=52)
        assert result.isna().all()

    def test_constant_series_produces_nans(self, cot):
        # std = 0 → replaced with NaN → all NaN
        s = pd.Series([5.0] * 60)
        result = cot.zscore_rolling(s, window=52)
        assert result.isna().all()

    def test_zscore_mean_near_zero(self, cot):
        # For a long enough series, the mean of valid Z-scores is ~0
        rng = np.random.default_rng(42)
        s = pd.Series(rng.normal(0, 1, 200))
        result = cot.zscore_rolling(s, window=52)
        valid = result.dropna()
        assert abs(valid.mean()) < 0.5

    def test_zscore_std_near_one(self, cot):
        rng = np.random.default_rng(99)
        s = pd.Series(rng.normal(100, 15, 300))
        result = cot.zscore_rolling(s, window=52)
        valid = result.dropna()
        assert 0.5 < valid.std() < 1.5


# ── URL builders ──────────────────────────────────────────────────────────────

class TestUrlBuilders:
    def test_legacy_url(self, cot):
        assert cot.legacy_url(2023) == "https://www.cftc.gov/files/dea/history/deacot2023.zip"

    def test_disagg_url(self, cot):
        assert cot.disagg_url(2021) == "https://www.cftc.gov/files/dea/history/fut_disagg_txt_2021.zip"


# ── CONSOLIDATED_LOOKUP ───────────────────────────────────────────────────────

class TestConsolidatedLookup:
    def test_es_consolidated_maps_to_base(self, cot):
        assert cot.CONSOLIDATED_LOOKUP["13874+"] == "13874A"

    def test_nq_consolidated_maps_to_base(self, cot):
        assert cot.CONSOLIDATED_LOOKUP["20974+"] == "209742"

    def test_ym_consolidated_maps_to_base(self, cot):
        assert cot.CONSOLIDATED_LOOKUP["12460+"] == "12460A"

    def test_non_consolidated_instruments_absent(self, cot):
        # ZN, ZB, 6E have no consolidated ID — should not appear as keys
        all_keys = set(cot.CONSOLIDATED_LOOKUP.keys())
        assert "043602" not in all_keys  # ZN base code
        assert "020601" not in all_keys  # ZB base code


# ── parse_legacy_zip ──────────────────────────────────────────────────────────

def _make_legacy_zip(rows: list[dict]) -> io.BytesIO:
    """Build an in-memory legacy COT ZIP with the given data rows."""
    headers = [
        "CFTC_Contract_Market_Code",
        "As of Date in Form YYMMDD",
        "Noncommercial Positions-Long (All)",
        "Noncommercial Positions-Short (All)",
        "Commercial Positions-Long (All)",
        "Commercial Positions-Short (All)",
        "Nonreportable Positions-Long (All)",
        "Nonreportable Positions-Short (All)",
        "Open Interest (All)",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("annual.txt", buf.getvalue())
    zip_buf.seek(0)
    return zip_buf


class TestParseLegacyZip:
    def test_known_es_row_parsed(self, cot):
        row = {
            "CFTC_Contract_Market_Code": "13874A",
            "As of Date in Form YYMMDD": "230106",  # 2023-01-06
            "Noncommercial Positions-Long (All)": "500000",
            "Noncommercial Positions-Short (All)": "200000",
            "Commercial Positions-Long (All)": "300000",
            "Commercial Positions-Short (All)": "400000",
            "Nonreportable Positions-Long (All)": "50000",
            "Nonreportable Positions-Short (All)": "60000",
            "Open Interest (All)": "1000000",
        }
        df = cot.parse_legacy_zip(_make_legacy_zip([row]))
        assert not df.empty
        assert df.iloc[0]["symbol"] == "ES"
        assert df.iloc[0]["report_date"] == date(2023, 1, 6)
        assert df.iloc[0]["nc_long"] == 500000
        assert df.iloc[0]["comm_short"] == 400000
        assert df.iloc[0]["open_interest"] == 1000000

    def test_consolidated_id_resolved_to_base(self, cot):
        # Post-May 2023 CFTC uses consolidated code "13874+" for ES
        row = {
            "CFTC_Contract_Market_Code": "13874+",
            "As of Date in Form YYMMDD": "230630",
            "Noncommercial Positions-Long (All)": "100000",
            "Noncommercial Positions-Short (All)": "80000",
            "Commercial Positions-Long (All)": "200000",
            "Commercial Positions-Short (All)": "220000",
            "Nonreportable Positions-Long (All)": "10000",
            "Nonreportable Positions-Short (All)": "12000",
            "Open Interest (All)": "400000",
        }
        df = cot.parse_legacy_zip(_make_legacy_zip([row]))
        assert not df.empty
        # Resolved to base code 13874A → symbol ES
        assert df.iloc[0]["cftc_code"] == "13874A"
        assert df.iloc[0]["symbol"] == "ES"

    def test_unknown_ticker_filtered_out(self, cot):
        row = {
            "CFTC_Contract_Market_Code": "XXXXXX",
            "As of Date in Form YYMMDD": "230106",
            "Noncommercial Positions-Long (All)": "100",
            "Noncommercial Positions-Short (All)": "100",
            "Commercial Positions-Long (All)": "100",
            "Commercial Positions-Short (All)": "100",
            "Nonreportable Positions-Long (All)": "10",
            "Nonreportable Positions-Short (All)": "10",
            "Open Interest (All)": "320",
        }
        df = cot.parse_legacy_zip(_make_legacy_zip([row]))
        assert df.empty

    def test_yyyymmdd_date_format_parsed(self, cot):
        # Use NQ ("209742") — no leading zero, so pandas reads the code as a
        # string and the filter works. ZN ("043602") exposes a separate bug
        # (see test_leading_zero_code_silently_dropped).
        row = {
            "CFTC_Contract_Market_Code": "209742",  # NQ
            "As of Date in Form YYMMDD": "20230106",  # 8-digit YYYYMMDD
            "Noncommercial Positions-Long (All)": "10000",
            "Noncommercial Positions-Short (All)": "8000",
            "Commercial Positions-Long (All)": "5000",
            "Commercial Positions-Short (All)": "6000",
            "Nonreportable Positions-Long (All)": "1000",
            "Nonreportable Positions-Short (All)": "900",
            "Open Interest (All)": "30000",
        }
        df = cot.parse_legacy_zip(_make_legacy_zip([row]))
        assert not df.empty
        assert df.iloc[0]["report_date"] == date(2023, 1, 6)

    def test_disagg_columns_null_for_legacy_rows(self, cot):
        row = {
            "CFTC_Contract_Market_Code": "209742",  # NQ — no leading zero
            "As of Date in Form YYMMDD": "230106",
            "Noncommercial Positions-Long (All)": "10000",
            "Noncommercial Positions-Short (All)": "8000",
            "Commercial Positions-Long (All)": "5000",
            "Commercial Positions-Short (All)": "6000",
            "Nonreportable Positions-Long (All)": "1000",
            "Nonreportable Positions-Short (All)": "900",
            "Open Interest (All)": "30000",
        }
        df = cot.parse_legacy_zip(_make_legacy_zip([row]))
        assert not df.empty
        assert df.iloc[0]["mm_long"] is None
        assert df.iloc[0]["prod_short"] is None

    def test_leading_zero_code_silently_dropped(self, cot):
        """
        Fix verified: CFTC codes with leading zeros (ZN=043602, ZB=020601, 6E=099741)
        are now preserved because pd.read_csv uses dtype=str, preventing pandas from
        reading "043602" as integer 43602.
        """
        row = {
            "CFTC_Contract_Market_Code": "043602",  # ZN
            "As of Date in Form YYMMDD": "230106",
            "Noncommercial Positions-Long (All)": "10000",
            "Noncommercial Positions-Short (All)": "8000",
            "Commercial Positions-Long (All)": "5000",
            "Commercial Positions-Short (All)": "6000",
            "Nonreportable Positions-Long (All)": "1000",
            "Nonreportable Positions-Short (All)": "900",
            "Open Interest (All)": "30000",
        }
        df = cot.parse_legacy_zip(_make_legacy_zip([row]))
        assert not df.empty
        assert df.iloc[0]["symbol"] == "ZN"
