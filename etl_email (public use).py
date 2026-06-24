"""
etl_email.py
============
Shared ETL completion notification utility.
Sends a plain-text summary email via Gmail SMTP after each ETL pipeline run.

Usage:
    from etl_email import send_etl_notification

    send_etl_notification(
        hub="COT Positioning",
        status="SUCCESS",
        rows_written=10234,
        latest_data_date="2026-06-20",
        duration_seconds=142,
        warnings=[],
        errors=[],
    )

Setup:
    Add these three variables to your .env file:
        GMAIL_APP_PASSWORD=your_16char_app_password
        ETL_EMAIL_FROM=your_gmail@gmail.com
        ETL_EMAIL_TO=destination@email.com

    Gmail App Passwords require 2-Step Verification to be enabled.
    Generate one at: Google Account -> Security -> App Passwords
"""

import os
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def send_etl_notification(
    hub,
    status,
    rows_written=None,
    latest_data_date=None,
    duration_seconds=None,
    warnings=None,
    errors=None,
    extra_lines=None,
):
    """
    Send a plain-text ETL completion email.

    Parameters
    ----------
    hub : str
        Pipeline name e.g. "COT Positioning", "Liquidity", "Macro Regime",
        "Market Sentiment", "DCF Valuation", "Backtest"
    status : str
        "SUCCESS" or "FAILED"
    rows_written : int, optional
        Total rows inserted/updated this run
    latest_data_date : str, optional
        Most recent data date in the target table e.g. "2026-06-20"
    duration_seconds : float, optional
        Total wall-clock runtime
    warnings : list of str, optional
        Non-fatal warnings encountered during the run
    errors : list of str, optional
        Errors encountered (used when status="FAILED")
    extra_lines : list of str, optional
        Any hub-specific stats to append e.g. ["Signals generated: 8,584"]
    """

    warnings    = warnings    or []
    errors      = errors      or []
    extra_lines = extra_lines or []

    # ── Load credentials from environment ────────────────────────────
    smtp_server    = "smtp.gmail.com"
    smtp_port      = 587
    email_from     = os.environ.get("ETL_EMAIL_FROM", "")
    email_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    email_to       = os.environ.get("ETL_EMAIL_TO", "")

    if not all([email_from, email_password, email_to]):
        print("  [EMAIL] Skipped — ETL_EMAIL_FROM / GMAIL_APP_PASSWORD / ETL_EMAIL_TO not set in .env")
        return

    # ── Build subject ─────────────────────────────────────────────────
    now    = datetime.now()
    emoji  = "OK" if status == "SUCCESS" else "FAIL"
    subject = f"[MIS ETL {emoji}] {hub} — {now.strftime('%a %Y-%m-%d %H:%M')}"

    # ── Build body ────────────────────────────────────────────────────
    lines = [
        f"Market Intelligence Suite — ETL Notification",
        f"{'=' * 50}",
        f"Hub        : {hub}",
        f"Status     : {status}",
        f"Run time   : {now.strftime('%Y-%m-%d %H:%M:%S')}",
    ]

    if duration_seconds is not None:
        mins, secs = divmod(int(duration_seconds), 60)
        lines.append(f"Duration   : {mins}m {secs}s")

    if rows_written is not None:
        lines.append(f"Rows       : {rows_written:,}")

    if latest_data_date is not None:
        lines.append(f"Latest date: {latest_data_date}")

    if extra_lines:
        lines.append("")
        lines.extend(extra_lines)

    if warnings:
        lines.append("")
        lines.append(f"Warnings ({len(warnings)}):")
        for w in warnings:
            lines.append(f"  - {w}")

    if errors:
        lines.append("")
        lines.append(f"Errors ({len(errors)}):")
        for e in errors:
            lines.append(f"  - {e}")

    if status == "SUCCESS" and not warnings:
        lines.append("")
        lines.append("Clean run — no warnings.")

    lines += [
        "",
        f"{'=' * 50}",
        "Market Intelligence Suite",
        "Auto-generated — do not reply",
    ]

    body = "\n".join(lines)

    # ── Send ──────────────────────────────────────────────────────────
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = email_from
        msg["To"]      = email_to
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(email_from, email_password)
            server.sendmail(email_from, email_to, msg.as_string())

        print(f"  [EMAIL] Notification sent to {email_to}")

    except Exception as e:
        # Email failure must never crash the ETL
        print(f"  [EMAIL] Send failed (non-fatal): {e}")
