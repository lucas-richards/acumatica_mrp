"""MRP formulas for purchased components.

All quantities are Decimals. A month is treated as 30 days throughout.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Iterable, Optional

from .models import (
    STATUS_RANK,
    BASIS_CUSTOM,
    BASIS_SIX,
    BASIS_TWO_YEAR,
    BomLine,
    ComponentInput,
    ComponentPlan,
    FgUsage,
    FinishedGood,
    Flag,
    PlanSummary,
    QtyMode,
    Status,
)

DAYS_PER_MONTH = Decimal(30)
ZERO = Decimal(0)
MAX_COVER_MONTHS = 4   # kept for the stored (now unused) cover setting
MIN_LEAD_DAYS = 7
MIN_PREPARE_DAYS = 7    # time to prepare an order after the lead time, at least
PREPARE_LEAD_FRACTION = Decimal(1) / 3   # the longer the lead time, the more time to prepare


def effective_lead_days(lead_time_days: Optional[int]) -> Decimal:
    """The lead time used for planning: never less than a week, even when none is set."""
    return Decimal(max(lead_time_days or 0, MIN_LEAD_DAYS))


def lead_time_months(lead_time_days: Optional[int]) -> Decimal:
    """The red line: the lead time, at least 7 days."""
    return effective_lead_days(lead_time_days) / DAYS_PER_MONTH


def demand_in_lead_time(usage_per_month: Decimal, lt_months: Decimal) -> Decimal:
    return usage_per_month * lt_months


def supply_position(on_hand: Decimal, on_order: Decimal, committed: Decimal,
                    in_receiving: Decimal = ZERO) -> Decimal:
    return on_hand + on_order + in_receiving - committed


def months_of_supply(supply: Decimal, usage_per_month: Decimal) -> Optional[Decimal]:
    """Months the supply position lasts. None means infinite (no usage)."""
    if usage_per_month <= 0:
        return None
    return supply / usage_per_month


def target_months(lead_time_days: Optional[int]) -> Decimal:
    """The reorder point: the lead time (at least 7 days) plus time to prepare the order, which is a
    third of the lead time and at least 7 days. A week or less of lead time gives 14 days; 3 months
    gives 4 months."""
    lead = effective_lead_days(lead_time_days)
    prepare = max(Decimal(MIN_PREPARE_DAYS), lead * PREPARE_LEAD_FRACTION)
    return (lead + prepare) / DAYS_PER_MONTH


MIN_ORDER_LEAD_FACTOR = Decimal('1.2')   # an order covers at least the lead time's demand plus 20%


def minimum_order(usage_per_month: Decimal, lt_months: Decimal) -> Decimal:
    """The least worth ordering once an order is needed: usage over the lead time, plus 20%."""
    return max(usage_per_month, ZERO) * lt_months * MIN_ORDER_LEAD_FACTOR


def needed_qty(usage_per_month: Decimal, target: Decimal, supply: Decimal) -> Decimal:
    return usage_per_month * target - supply


def round_up_to_lot(qty: Decimal, lot_size: Optional[Decimal]) -> Decimal:
    """Round a positive need up to a whole number of lots; 0 if nothing is needed.

    A missing or non-positive lot size is treated as 1.
    """
    if qty <= 0:
        return ZERO
    lot = lot_size if lot_size and lot_size > 0 else Decimal(1)
    lots = (qty / lot).to_integral_value(rounding=ROUND_CEILING)
    return lots * lot


def order_by_date(
    today: dt.date,
    supply: Decimal,
    usage_per_month: Decimal,
    lead_time_days: Optional[int],
) -> Optional[dt.date]:
    """today + (months_of_supply - lead_time_months) x 30 days.

    Computed as supply/usage x 30 - lead_time_days to avoid rounding through
    months; fractional days are floored (order a day early rather than late).
    Returns None when there is no usage or the date is beyond the calendar.
    """
    if usage_per_month <= 0:
        return None
    days = supply / usage_per_month * DAYS_PER_MONTH - Decimal(lead_time_days or 0)
    whole_days = int(days.to_integral_value(rounding=ROUND_FLOOR))
    if whole_days > (dt.date.max - today).days:
        return None      # so far out it never needs ordering
    return today + dt.timedelta(days=max(whole_days, (dt.date.min - today).days))


def plan_status(mos: Optional[Decimal], lt_months: Decimal, target: Decimal,
                supply: Decimal = ZERO) -> Status:
    if supply < 0:   # already short (e.g. negative available), whatever the usage
        return Status.STOCKOUT_RISK
    if mos is None:
        return Status.COVERED
    if mos < lt_months:
        return Status.STOCKOUT_RISK
    if mos < target:
        return Status.ORDER_SOON
    return Status.COVERED


def build_usage(
    component_sku: str,
    bom_lines: Iterable[BomLine],
    finished_goods: dict[str, FinishedGood],
) -> list[FgUsage]:
    """Each finished good that uses the component, with the open orders for it that have no production order."""
    rows: list[FgUsage] = []
    for line in bom_lines:
        if line.component_sku != component_sku:
            continue
        fg = finished_goods.get(line.fg_sku) or FinishedGood(sku=line.fg_sku)
        rows.append(FgUsage(
            fg_sku=fg.sku,
            fg_description=fg.description,
            qty_per=line.qty_per,
            fg_demand_per_month=fg.demand_per_month,
            fg_on_hand=fg.on_hand,
            last_run_qty=fg.last_run_qty,
            last_run_date=fg.last_run_date,
            fg_unproduced_orders=fg.unproduced_orders,
            component_qty=fg.unproduced_orders * line.qty_per,
        ))
    rows.sort(key=lambda r: (-r.component_qty, r.fg_sku))
    return rows


def chosen_usage(component: ComponentInput) -> tuple[Decimal, str]:
    """The monthly usage the planner chose for this item, and the basis it came from. A choice with no
    figure behind it (no history, or a custom usage that was cleared) falls back to the recent usage."""
    recent = component.issue_demand_per_month or ZERO
    if component.usage_basis == BASIS_TWO_YEAR and component.long_term_usage is not None:
        return component.long_term_usage, BASIS_TWO_YEAR
    if component.usage_basis == BASIS_CUSTOM and component.custom_usage is not None:
        return component.custom_usage, BASIS_CUSTOM
    return recent, BASIS_SIX


def plan_component(
    component: ComponentInput,
    bom_lines: Iterable[BomLine],
    finished_goods: dict[str, FinishedGood],
    qty_mode: QtyMode,
    today: dt.date,
) -> ComponentPlan:
    flags: set[Flag] = set()
    if component.lead_time_days is None:
        flags.add(Flag.MISSING_LEAD_TIME)
    if not component.vendor_id:
        flags.add(Flag.NO_VENDOR)

    lt_months = lead_time_months(component.lead_time_days)
    # A bought finished good has nothing built from it to show
    breakdown = [] if component.is_finished_good else build_usage(component.sku, bom_lines, finished_goods)
    if component.earliest_promised and component.earliest_promised < today:
        flags.add(Flag.PO_LATE)
    # Vendor set-up warnings, unless someone marked them done within the last week
    if component.has_vendor_rows and not component.default_vendor_id:
        if Flag.NO_DEFAULT_VENDOR.value not in component.acked_warnings:
            flags.add(Flag.NO_DEFAULT_VENDOR)
    elif (component.default_vendor_id and component.last_po_vendor_id
          and component.last_po_vendor_id.strip().upper() != component.default_vendor_id.strip().upper()):
        if Flag.VENDOR_MISMATCH.value not in component.acked_warnings:
            flags.add(Flag.VENDOR_MISMATCH)

    # Usage is the issue history. Open orders for finished goods with no production order yet are not in
    # that history, so the components they need are committed against supply.
    committed = sum((r.component_qty for r in breakdown), ZERO)
    has_history = component.issue_demand_per_month is not None
    usage, basis = chosen_usage(component)
    if not breakdown and not has_history and not component.is_finished_good:
        flags.add(Flag.NO_BOM)
    if (breakdown or has_history) and usage <= 0:
        flags.add(Flag.NO_USAGE)

    supply = supply_position(component.on_hand, component.on_order, committed, component.in_receiving)
    mos = months_of_supply(supply, usage)
    target = target_months(component.lead_time_days)
    needed = needed_qty(usage, target, supply)
    suggested = round_up_to_lot(needed, component.lot_size)
    if suggested > 0:
        # An order is never smaller than the lead time's demand plus 20%, whatever is already in stock
        suggested = max(suggested, round_up_to_lot(minimum_order(usage, lt_months), component.lot_size))

    if suggested > 0 and qty_mode == QtyMode.MATCH_LAST_PO:
        if component.last_po_qty and component.last_po_qty > 0:
            suggested = component.last_po_qty
        else:
            flags.add(Flag.NO_PO_HISTORY)

    obd = order_by_date(today, supply, usage, component.lead_time_days)
    status = plan_status(mos, lt_months, target, supply)

    return ComponentPlan(
        component=component,
        breakdown=breakdown,
        usage_per_month=usage,
        usage_basis=basis,
        lead_time_months=lt_months,
        demand_in_lead_time=demand_in_lead_time(usage, lt_months),
        committed=committed,
        supply_position=supply,
        months_of_supply=mos,
        target_months=target,
        needed=needed,
        suggested_qty=suggested,
        order_by_date=obd,
        order_now=obd is not None and obd <= today,
        status=status,
        flags=flags,
    )


def urgency_key(plan: ComponentPlan):
    far_future = dt.date.max
    return (
        STATUS_RANK[plan.status],
        plan.order_by_date or far_future,
        plan.months_of_supply if plan.months_of_supply is not None else Decimal('Infinity'),
        plan.sku,
    )


def plan_components(
    components: Iterable[ComponentInput],
    bom_lines: Iterable[BomLine],
    finished_goods: Iterable[FinishedGood],
    qty_mode: QtyMode,
    today: dt.date,
) -> list[ComponentPlan]:
    """Plan every component and return them sorted most urgent first."""
    bom_by_component: dict[str, list[BomLine]] = defaultdict(list)
    for line in bom_lines:
        bom_by_component[line.component_sku].append(line)
    fg_map = {fg.sku: fg for fg in finished_goods}
    plans = [
        plan_component(c, bom_by_component.get(c.sku, []), fg_map, qty_mode, today)
        for c in components
    ]
    plans.sort(key=urgency_key)
    return plans


def summarize(plans: list[ComponentPlan]) -> PlanSummary:
    to_order = [p for p in plans if p.needs_order]
    order_dates = [p.order_by_date for p in to_order if p.order_by_date is not None]
    next_date = min(order_dates) if order_dates else None
    return PlanSummary(
        shown=len(plans),
        components_to_order=len(to_order),
        out_before_arrival=[p.sku for p in plans if p.status == Status.STOCKOUT_RISK],
        next_order_by=next_date,
        next_order_now=any(p.order_now for p in to_order),
        late_count=sum(1 for p in to_order if p.order_now),
        vendor_count=len({p.component.vendor_id for p in to_order if p.component.vendor_id}),
    )
