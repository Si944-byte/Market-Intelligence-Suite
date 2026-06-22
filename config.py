"""
config.py — All magic numbers and thresholds for the MIS ETL suite.
Import with: from config import COT, SENTIMENT, DCF, MACRO, LIQUIDITY
"""

COT = {
    "zscore_window":          52,       # weeks
    "zscore_min_periods":     10,
    "smooth_window":          13,       # 13-week smoothed Z for trend chart
    "neutral_threshold":      0.5,
    "extreme_threshold":      1.5,
    "divergence_threshold":   1.0,      # |Z| on both sides to trigger divergence signal
}

SENTIMENT = {
    "zscore_window":          252,      # trading days (1 year)
    "zscore_min_periods":     60,
    "neutral_threshold":      0.5,
    "extreme_threshold":      1.5,
    "fg_synthetic_clamp":     3.0,      # clip composite Z to ±3σ before mapping to 0–100
}

DCF = {
    "terminal_growth":        0.025,    # 2.5% perpetual growth
    "projection_years":       5,
    "buy_threshold":          0.10,     # valuation gap > +10% → BUY
    "sell_threshold":        -0.10,     # valuation gap < -10% → SELL
    "conservative_growth":    0.05,
    "conservative_discount":  0.10,
    "aggressive_growth":      0.15,
    "aggressive_discount":    0.07,
    "de_ratio_divisor":       100,      # API returns D/E as %; divide when > 20
    "de_ratio_threshold":     20,
}

MACRO = {
    "cpi_threshold":          3.0,      # % — above = inflationary regime
    "gdp_threshold":          2.0,      # % — below = contraction
    "gdp_smooth_window":      6,        # months
    "cpi_smooth_window":      3,        # months
    "history_start":          "2010-01-01",
}

LIQUIDITY = {
    "fred_history_start":     "2002-01-01",
    "dimdate_start":          "2002-01-01",
    "dimdate_end":            "2035-12-31",
    "expanding_threshold":    0.75,     # composite score
    "contracting_threshold": -1.00,
    "hy_zscore_window":       504,      # ~2 years of trading days
    "net_liq_weight":         0.50,
    "hy_credit_weight":       0.30,
    "yield_curve_weight":     0.20,
}
