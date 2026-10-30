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
MAX_COVER_MONTHS = 4


def lead_time_months(lead_time_days: Optional[int]) -> Decimal:
    if not lead_time_days or lead_time_days < 0:
        return ZERO
    return Decimal(lead_time_days) / DAYS_PER_MONTH


def demand_in_lead_time(usage_per_month: Decimal, lt_months: Decimal) -> Decimal:
    return usage_per_month * lt_months


def supply_position(on_hand: Decimal, on_order: Decimal, embedded_stock: Decimal) -> Decimal:
    return on_hand + on_order + embedded_stock


def months_of_supply(supply: Decimal, usage_per_month: Decimal) -> Optional[Decimal]:
    """Months the supply position lasts. None means infinite (no usage)."""
    if usage_per_month <= 0:
        return None
    return supply / usage_per_month


def target_months(lt_months: Decimal, cover_months: int) -> Decimal:
    if not 0 <= cover_months <= MAX_COVER_MONTHS:
        raise ValueError(f'cover_months must be 0..{MAX_COVER_MONTHS}, got {cover_months}')
    return lt_months + Decimal(cover_months)


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
    Returns None when there is no usage.
    """
    if usage_per_month <= 0:
        return None
    days = supply / usage_per_month * DAYS_PER_MONTH - Decimal(lead_time_days or 0)
    whole_days = int(days.to_integral_value(rounding=ROUND_FLOOR))
    return today + dt.timedelta(days=whole_days)


def plan_status(mos: Optional[Decimal], lt_months: Decimal, target: Decimal) -> Status:
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
    lt_months: Decimal,
) -> tuple[list[FgUsage], set[Flag]]:
    """Breakdown of each finished good that uses the component."""
    rows: list[FgUsage] = []
    flags: set[Flag] = set()
    for line in bom_lines:
        if line.component_sku != component_sku:
            continue
        fg = finished_goods.get(line.fg_sku) or FinishedGood(sku=line.fg_sku)
        if fg.demand_per_month is None:
            flags.add(Flag.MISSING_FG_DEMAND)
        fg_demand = fg.demand_per_month or ZERO
        per_month = fg_demand * line.qty_per
        rows.append(FgUsage(
            fg_sku=fg.sku,
            fg_description=fg.description,
            qty_per=line.qty_per,
            fg_demand_per_month=fg_demand,
            fg_on_hand=fg.on_hand,
            component_per_month=per_month,
            demand_in_lead_time=per_month * lt_months,
            embedded_qty=fg.on_hand * line.qty_per,
        ))
    rows.sort(key=lambda r: (-r.component_per_month, r.fg_sku))
    return rows, flags


def plan_component(
    component: ComponentInput,
    bom_lines: Iterable[BomLine],
    finished_goods: dict[str, FinishedGood],
    cover_months: int,
    qty_mode: QtyMode,
    today: dt.date,
) -> ComponentPlan:
    flags: set[Flag] = set()
    if component.lead_time_days is None:
        flags.add(Flag.MISSING_LEAD_TIME)
    if not component.vendor_id:
        flags.add(Flag.NO_VENDOR)

    lt_months = lead_time_months(component.lead_time_days)
    breakdown, usage_flags = build_usage(component.sku, bom_lines, finished_goods, lt_months)
    flags |= usage_flags
    if not breakdown:
        flags.add(Flag.NO_BOM)

    usage = sum((r.component_per_month for r in breakdown), ZERO)
    embedded = sum((r.embedded_qty for r in breakdown), ZERO)
    if breakdown and usage <= 0:
        flags.add(Flag.NO_USAGE)

    supply = supply_position(component.on_hand, component.on_order, embedded)
    mos = months_of_supply(supply, usage)
    target = target_months(lt_months, cover_months)
    needed = needed_qty(usage, target, supply)
    suggested = round_up_to_lot(needed, component.lot_size)

    if suggested > 0 and qty_mode == QtyMode.MATCH_LAST_PO:
        if component.last_po_qty and component.last_po_qty > 0:
            suggested = component.last_po_qty
        else:
            flags.add(Flag.NO_PO_HISTORY)

    if component.unit_cost is None:
        estimated_cost = None
        if suggested > 0:
            flags.add(Flag.MISSING_COST)
    else:
        estimated_cost = suggested * component.unit_cost

    obd = order_by_date(today, supply, usage, component.lead_time_days)
    status = plan_status(mos, lt_months, target)

    return ComponentPlan(
        component=component,
        breakdown=breakdown,
        usage_per_month=usage,
        lead_time_months=lt_months,
        demand_in_lead_time=demand_in_lead_time(usage, lt_months),
        embedded_stock=embedded,
        supply_position=supply,
        months_of_supply=mos,
        target_months=target,
        needed=needed,
        suggested_qty=suggested,
        estimated_cost=estimated_cost,
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
    cover_months: int,
    qty_mode: QtyMode,
    today: dt.date,
) -> list[ComponentPlan]:
    """Plan every component and return them sorted most urgent first."""
    bom_by_component: dict[str, list[BomLine]] = defaultdict(list)
    for line in bom_lines:
        bom_by_component[line.component_sku].append(line)
    fg_map = {fg.sku: fg for fg in finished_goods}
    plans = [
        plan_component(c, bom_by_component.get(c.sku, []), fg_map, cover_months, qty_mode, today)
        for c in components
    ]
    plans.sort(key=urgency_key)
    return plans


def summarize(plans: list[ComponentPlan]) -> PlanSummary:
    to_order = [p for p in plans if p.needs_order]
    order_dates = [p.order_by_date for p in to_order if p.order_by_date is not None]
    next_date = min(order_dates) if order_dates else None
    costed = [p for p in to_order if p.estimated_cost is not None]
    return PlanSummary(
        shown=len(plans),
        components_to_order=len(to_order),
        out_before_arrival=[p.sku for p in plans if p.status == Status.STOCKOUT_RISK],
        next_order_by=next_date,
        next_order_now=any(p.order_now for p in to_order),
        late_count=sum(1 for p in to_order if p.order_now),
        estimated_value=sum((p.estimated_cost for p in costed), ZERO),
        vendor_count=len({p.component.vendor_id for p in to_order if p.component.vendor_id}),
        missing_cost_count=len(to_order) - len(costed),
    )
