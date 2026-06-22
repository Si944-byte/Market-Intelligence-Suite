"""
conftest.py
-----------
Loads each ETL module by file path (filenames contain spaces, so normal
`import` syntax won't work).  Heavy dependencies that require live
connections (pyodbc, fredapi, yfinance) are stubbed before each module
is executed, so the test suite runs without any external services.
"""

import sys
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent


def _load(slug: str, relpath: str):
    """
    Import an ETL module by file path.
    Stubs out connection-heavy packages and suppresses log-file creation.
    """
    filepath = ROOT / relpath

    # Stub packages that require installed drivers or live credentials
    for pkg in ("pyodbc", "fredapi", "yfinance"):
        sys.modules.setdefault(pkg, MagicMock())

    # Prevent module-level logging.basicConfig from creating a real log file
    with patch("logging.FileHandler", return_value=MagicMock()):
        spec = importlib.util.spec_from_file_location(slug, str(filepath))
        module = importlib.util.module_from_spec(spec)
        sys.modules[slug] = module
        spec.loader.exec_module(module)

    return module


@pytest.fixture(scope="session")
def cot():
    return _load("cot_etl", Path("COT_Hub") / "cot_etl - public use.py")


@pytest.fixture(scope="session")
def dcf():
    return _load("calculate_dcf", Path("DCF_Hub") / "calculate_dcf (public use).py")


@pytest.fixture(scope="session")
def fetch():
    return _load(
        "fetch_fundamentals",
        Path("DCF_Hub") / "fetch_fundamentals_rapidapi (public use).py",
    )


@pytest.fixture(scope="session")
def sentiment():
    return _load(
        "sentiment_etl", Path("Sentiment_Hub") / "sentiment_etl (public use).py"
    )


@pytest.fixture(scope="session")
def liquidity():
    return _load(
        "liquidity_etl", Path("Liquidity_Hub") / "liquidity_etl (public use).py"
    )


@pytest.fixture(scope="session")
def macro():
    return _load("macro_etl", Path("Macro_Inflation_Watch") / "etl (public use).py")
