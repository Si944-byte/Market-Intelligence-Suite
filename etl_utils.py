"""
etl_utils.py — Shared utilities for all MIS ETL pipelines.
Import with: from etl_utils import managed_conn, fetch_with_retry, ...
"""

import contextlib
import logging
import os
import time

import numpy as np
import pyodbc


# ── Connection helpers ────────────────────────────────────────────────────────

def get_conn(server, database, user, password):
    """Return a raw pyodbc connection (caller manages lifecycle)."""
    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={server};DATABASE={database};"
        f"UID={user};PWD={password};TrustServerCertificate=yes",
        autocommit=False,
    )


@contextlib.contextmanager
def managed_conn(server, database, user, password):
    """
    Context manager for pyodbc connections.
    Commits on clean exit, rolls back on exception, always closes.

    Usage:
        with managed_conn(SERVER, DB, USER, PASSWORD) as conn:
            do_work(conn)
    """
    conn = get_conn(server, database, user, password)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Retry helper ──────────────────────────────────────────────────────────────

def fetch_with_retry(fn, max_attempts=3, base_wait=5, logger=None):
    """
    Call fn() up to max_attempts times with exponential backoff.
    Waits: 5s -> 10s -> 20s (base_wait * 2^attempt).
    Raises the last exception if all attempts fail.

    Usage:
        data = fetch_with_retry(lambda: requests.get(url, timeout=30))
    """
    log = logger or logging.getLogger(__name__)
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            wait = base_wait * (2 ** attempt)
            log.warning(
                f"Attempt {attempt + 1}/{max_attempts} failed: {e}. "
                f"Retrying in {wait}s..."
            )
            time.sleep(wait)


# ── Safe scalar converters ────────────────────────────────────────────────────

def safe_int(val):
    """Convert val to int, returning None for None/NaN/inf/empty/invalid."""
    if val is None:
        return None
    if hasattr(val, "item"):
        val = val.item()
    if isinstance(val, float):
        if np.isnan(val) or np.isinf(val):
            return None
        return int(val)
    try:
        cleaned = str(val).replace(",", "").strip()
        if cleaned.lower() in ("nan", "inf", "-inf", "none", ""):
            return None
        return int(float(cleaned))
    except Exception:
        return None


def safe_float(val):
    """Convert val to float (4 dp), returning None for None/NaN/inf/invalid."""
    if val is None:
        return None
    try:
        v = val.item() if hasattr(val, "item") else val
        f = float(v)
        if np.isnan(f) or np.isinf(f):
            return None
        return round(f, 4)
    except (ValueError, TypeError):
        return None


# ── Logging setup ─────────────────────────────────────────────────────────────

def configure_logging(log_path, logger_name=__name__, level=logging.INFO):
    """
    Set up a logger with file + console handlers.
    Safe to call multiple times — skips setup if handlers already exist.

    Usage:
        log = configure_logging("C:/path/to/etl.log", "cot_etl")
    """
    log = logging.getLogger(logger_name)
    log.setLevel(level)

    if log.handlers:
        return log  # already configured — avoid duplicate handlers

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(ch)

    return log
