"""Pure metric math: no I/O, so it's trivially testable and shared by the API and the agent."""

from __future__ import annotations

from datetime import date


def safe_ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def yoy_growth(current: float | None, prior: float | None) -> float | None:
    """Fractional growth; None when the base is missing or zero. Uses |prior| so a move
    from a loss to a smaller loss reads as positive growth."""
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / abs(prior)


def consecutive_years(prior_end: date, current_end: date) -> bool:
    """Guard YoY against gaps or fiscal-year-end changes (52/53-week years are fine)."""
    return 330 <= (current_end - prior_end).days <= 400


def split_factor(splits: list[tuple[date, float]], after: date) -> float:
    """Cumulative split ratio for splits strictly after `after`.

    EPS is reported on the share count at filing time, while Yahoo prices are
    split-adjusted to today. Dividing EPS by this factor puts both on today's basis.
    """
    factor = 1.0
    for d, ratio in splits:
        if d > after and ratio > 0:
            factor *= ratio
    return factor


def pe_ratio(price: float | None, eps: float | None) -> float | None:
    """Trailing P/E; None when earnings are non-positive (not meaningful)."""
    if price is None or eps is None or eps <= 0:
        return None
    return price / eps


def fmt_money(v: float | None) -> str | None:
    if v is None:
        return None
    sign = "-" if v < 0 else ""
    a = abs(v)
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if a >= div:
            return f"{sign}${a / div:,.2f}{suffix}"
    return f"{sign}${a:,.2f}"


def fmt_pct(v: float | None, digits: int = 1) -> str | None:
    return None if v is None else f"{v * 100:.{digits}f}%"


def fmt_value(v: float | None, unit: str) -> str | None:
    if v is None:
        return None
    if unit == "USD":
        return fmt_money(v)
    if unit == "USD/shares":
        return f"${v:,.2f}"
    if unit == "ratio":
        return fmt_pct(v)
    if unit == "shares":
        return f"{v / 1e9:,.3f}B shares" if abs(v) >= 1e9 else f"{v / 1e6:,.1f}M shares"
    return f"{v:,.4g}"
