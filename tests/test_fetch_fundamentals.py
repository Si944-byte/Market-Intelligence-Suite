"""
Tests for DCF_Hub/fetch_fundamentals_rapidapi (public use).py

Covers:
  - raw()               (extract {raw: value} from Yahoo Finance JSON)
  - safe_float()        (safe numeric conversion)
  - D/E ratio logic     (API returns percentage; >20 → divide by 100)
  - get_already_fetched (resume support via in-memory SQLite)
  - init_db             (idempotent table creation)
"""

import sqlite3

import pytest


# ── raw() ─────────────────────────────────────────────────────────────────────

class TestRaw:
    def test_nested_dict_returns_raw_value(self, fetch):
        data = {"currentPrice": {"raw": 150.0, "fmt": "$150.00"}}
        assert fetch.raw(data, "currentPrice") == 150.0

    def test_plain_numeric_returned_directly(self, fetch):
        data = {"currentPrice": 150.0}
        assert fetch.raw(data, "currentPrice") == 150.0

    def test_plain_int_returned_directly(self, fetch):
        data = {"marketCap": 1_000_000_000}
        assert fetch.raw(data, "marketCap") == 1_000_000_000

    def test_missing_key_returns_none(self, fetch):
        assert fetch.raw({}, "currentPrice") is None

    def test_none_value_returns_none(self, fetch):
        data = {"currentPrice": None}
        assert fetch.raw(data, "currentPrice") is None

    def test_dict_without_raw_key_returns_none(self, fetch):
        # {fmt: ...} with no "raw" key
        data = {"currentPrice": {"fmt": "$150.00"}}
        assert fetch.raw(data, "currentPrice") is None

    def test_string_value_returns_none(self, fetch):
        # Strings are not int/float → returns None
        data = {"sector": "Technology"}
        assert fetch.raw(data, "sector") is None


# ── safe_float() ──────────────────────────────────────────────────────────────

class TestSafeFloat:
    def test_float_passthrough(self, fetch):
        assert fetch.safe_float(3.14) == 3.14

    def test_int_converted(self, fetch):
        assert fetch.safe_float(42) == 42.0

    def test_none_returns_none(self, fetch):
        assert fetch.safe_float(None) is None

    def test_invalid_string_returns_none(self, fetch):
        assert fetch.safe_float("not_a_number") is None

    def test_numeric_string_converted(self, fetch):
        # The bare except swallows TypeError but float("3.5") works
        assert fetch.safe_float("3.5") == 3.5


# ── Debt-to-Equity conversion logic ──────────────────────────────────────────

class TestDebtToEquityConversion:
    """
    The API returns D/E as a percentage (e.g. 102.63 = 102.63%).
    Values > 20 are divided by 100 to convert to a ratio.
    """

    @staticmethod
    def _convert(de_raw, safe_float_fn):
        de = safe_float_fn(de_raw)
        if de and de > 20:
            de = de / 100
        return de

    def test_high_percentage_converted_to_ratio(self, fetch):
        # 102.63% → 1.0263
        result = self._convert(102.63, fetch.safe_float)
        assert result == pytest.approx(1.0263, rel=1e-4)

    def test_low_ratio_unchanged(self, fetch):
        # 1.5 → 1.5 (not > 20)
        result = self._convert(1.5, fetch.safe_float)
        assert result == pytest.approx(1.5)

    def test_boundary_exactly_20_unchanged(self, fetch):
        # 20.0 → NOT > 20 → stays 20.0
        result = self._convert(20.0, fetch.safe_float)
        assert result == pytest.approx(20.0)

    def test_boundary_20_1_converted(self, fetch):
        result = self._convert(20.1, fetch.safe_float)
        assert result == pytest.approx(0.201, rel=1e-4)

    def test_none_de_stays_none(self, fetch):
        result = self._convert(None, fetch.safe_float)
        assert result is None


# ── get_already_fetched + init_db ─────────────────────────────────────────────

class TestGetAlreadyFetched:
    @pytest.fixture
    def db(self, fetch):
        """In-memory SQLite with the fundamentals schema initialised."""
        conn = sqlite3.connect(":memory:")
        fetch.init_db(conn)
        return conn

    def test_empty_db_returns_empty_set(self, fetch, db):
        result = fetch.get_already_fetched(db, "2024-01-01")
        assert result == set()

    def test_inserted_ticker_returned(self, fetch, db):
        db.execute(
            "INSERT INTO fundamentals "
            "(fetch_date, ticker, current_price, created_at) "
            "VALUES ('2024-01-01', 'AAPL', 185.0, '2024-01-01 10:00:00')"
        )
        db.commit()
        result = fetch.get_already_fetched(db, "2024-01-01")
        assert "AAPL" in result

    def test_different_date_not_returned(self, fetch, db):
        db.execute(
            "INSERT INTO fundamentals "
            "(fetch_date, ticker, current_price, created_at) "
            "VALUES ('2024-01-02', 'MSFT', 400.0, '2024-01-02 10:00:00')"
        )
        db.commit()
        result = fetch.get_already_fetched(db, "2024-01-01")
        assert "MSFT" not in result

    def test_multiple_tickers_all_returned(self, fetch, db):
        for ticker in ["AAPL", "MSFT", "GOOG"]:
            db.execute(
                "INSERT INTO fundamentals "
                "(fetch_date, ticker, current_price, created_at) VALUES (?,?,?,?)",
                ("2024-01-01", ticker, 100.0, "2024-01-01 10:00:00"),
            )
        db.commit()
        result = fetch.get_already_fetched(db, "2024-01-01")
        assert result == {"AAPL", "MSFT", "GOOG"}

    def test_init_db_idempotent(self, fetch, db):
        # Running init_db twice should not raise
        fetch.init_db(db)
        result = fetch.get_already_fetched(db, "2024-01-01")
        assert result == set()
