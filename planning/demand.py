"""Finished-goods monthly demand sources.

Every source has the same signature so they can be swapped by name:

    fn(buckets, today, months) -> {fg_sku: demand_per_month}

`buckets` are monthly quantities for one source (shipped qty, open sales-order
qty by requested month, or forecast qty), keyed by the first day of the month.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Iterable


@dataclass(frozen=True)
class MonthlyQty:
    sku: str
    month: dt.date      # first day of the month
    qty: Decimal


DemandFn = Callable[[Iterable[MonthlyQty], dt.date, int], dict[str, Decimal]]


def month_start(day: dt.date) -> dt.date:
    return day.replace(day=1)


def add_months(month: dt.date, n: int) -> dt.date:
    index = month.year * 12 + (month.month - 1) + n
    return dt.date(index // 12, index % 12 + 1, 1)


def _sum_in_window(buckets, start: dt.date | None, end: dt.date) -> dict[str, Decimal]:
    """Sum quantities with start <= month < end (start None = no lower bound)."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for b in buckets:
        m = month_start(b.month)
        if (start is None or m >= start) and m < end:
            totals[b.sku] += b.qty
    return totals


def trailing_invoices(buckets: Iterable[MonthlyQty], today: dt.date, months: int = 6) -> dict[str, Decimal]:
    """Average invoiced (or, for ISSUE_SOURCE, issued) quantity over the last `months` full calendar months.

    Months with no activity count as zero, so a 6-month window always divides by 6.
    """
    _check_months(months)
    current = month_start(today)
    totals = _sum_in_window(buckets, add_months(current, -months), current)
    return {sku: total / months for sku, total in totals.items()}


def usage_history(buckets: Iterable[MonthlyQty], today: dt.date,
                  months: int = 24) -> dict[str, tuple[Decimal, int]]:
    """(average per month, months with any use) over the last `months` full calendar months, per sku.
    Rows of several sources can be passed together; a month's rows are added up first. Shows whether
    usage is steady or comes in bursts."""
    _check_months(months)
    current = month_start(today)
    start = add_months(current, -months)
    per_month: dict[tuple[str, dt.date], Decimal] = defaultdict(Decimal)
    for b in buckets:
        m = month_start(b.month)
        if start <= m < current:
            per_month[(b.sku, m)] += b.qty
    totals: dict[str, Decimal] = defaultdict(Decimal)
    active: dict[str, int] = defaultdict(int)
    for (sku, _), qty in per_month.items():
        totals[sku] += qty
        if qty > 0:
            active[sku] += 1
    return {sku: (total / months, active[sku]) for sku, total in totals.items()}


def open_sales_orders(buckets: Iterable[MonthlyQty], today: dt.date, months: int = 3) -> dict[str, Decimal]:
    """Open sales-order quantity requested before the end of the horizon, spread per month.

    Past-due lines are included (they still have to ship).
    """
    _check_months(months)
    end = add_months(month_start(today), months)
    totals = _sum_in_window(buckets, None, end)
    return {sku: total / months for sku, total in totals.items()}


def forecast(buckets: Iterable[MonthlyQty], today: dt.date, months: int = 3) -> dict[str, Decimal]:
    """Average forecast quantity for the next `months` months, starting with the current one."""
    _check_months(months)
    current = month_start(today)
    totals = _sum_in_window(buckets, current, add_months(current, months))
    return {sku: total / months for sku, total in totals.items()}


def _check_months(months: int) -> None:
    if months < 1:
        raise ValueError('months must be at least 1')


DEMAND_SOURCES: dict[str, DemandFn] = {
    'trailing_invoices': trailing_invoices,
    'open_sales_orders': open_sales_orders,
    'forecast': forecast,
}

# Component consumption (inventory issues, net of returns). Not a selectable finished-good source:
# it is always synced and sets the monthly usage of purchased components.
ISSUE_SOURCE = 'trailing_issues'
# Quantity consumed by kit assemblies ("assembly out"). Added to issues for components and to invoices
# for finished goods that are themselves used as kit components.
ASSEMBLY_SOURCE = 'trailing_assembly'
# Open sales-order and open production-order quantity (by due month), shown as "SO/PO demand"
OPEN_SO_SOURCE = 'open_so'
OPEN_PO_SOURCE = 'open_po'
INTERNAL_SOURCES = (ISSUE_SOURCE, ASSEMBLY_SOURCE, OPEN_SO_SOURCE, OPEN_PO_SOURCE)

DEFAULT_WINDOW_MONTHS = {
    'trailing_invoices': 6,
    'open_sales_orders': 3,
    'forecast': 3,
}


def get_demand_function(name: str) -> DemandFn:
    if name in (ISSUE_SOURCE, ASSEMBLY_SOURCE):
        return trailing_invoices
    try:
        return DEMAND_SOURCES[name]
    except KeyError:
        raise ValueError(f'Unknown demand source {name!r}; choose from {sorted(DEMAND_SOURCES)}') from None
