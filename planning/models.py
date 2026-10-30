"""Plain data structures used as inputs and outputs of the MRP calculations."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional


class QtyMode(str, Enum):
    COVER_TARGET = 'cover'      # order enough to reach lead time + cover months
    MATCH_LAST_PO = 'last_po'   # reuse the previous PO quantity


class Status(str, Enum):
    STOCKOUT_RISK = 'Stockout risk'
    ORDER_SOON = 'Order soon'
    COVERED = 'Covered'


# Sort order for urgency (lower = more urgent)
STATUS_RANK = {Status.STOCKOUT_RISK: 0, Status.ORDER_SOON: 1, Status.COVERED: 2}


class Flag(str, Enum):
    NO_USAGE = 'no_usage'                    # used in BOMs but FG demand is zero
    NO_BOM = 'no_bom'                        # not a component of any finished good
    MISSING_FG_DEMAND = 'missing_fg_demand'  # a parent FG has no demand data
    MISSING_LEAD_TIME = 'missing_lead_time'  # no lead time from override, item-vendor or vendor
    NO_VENDOR = 'no_vendor'
    MISSING_COST = 'missing_cost'
    NO_PO_HISTORY = 'no_po_history'          # "Match last PO" requested but no previous PO


FLAG_LABELS = {
    Flag.NO_USAGE: 'No usage',
    Flag.NO_BOM: 'Not in any BOM',
    Flag.MISSING_FG_DEMAND: 'FG demand missing',
    Flag.MISSING_LEAD_TIME: 'Lead time missing',
    Flag.NO_VENDOR: 'No vendor',
    Flag.MISSING_COST: 'Cost missing',
    Flag.NO_PO_HISTORY: 'No PO history',
}


@dataclass(frozen=True)
class FinishedGood:
    sku: str
    description: str = ''
    on_hand: Decimal = Decimal('0')
    demand_per_month: Optional[Decimal] = None   # None = no demand data


@dataclass(frozen=True)
class BomLine:
    fg_sku: str
    component_sku: str
    qty_per: Decimal


@dataclass(frozen=True)
class ComponentInput:
    sku: str
    description: str = ''
    vendor_id: Optional[str] = None
    vendor_name: Optional[str] = None
    on_hand: Decimal = Decimal('0')
    on_order: Decimal = Decimal('0')
    lead_time_days: Optional[int] = None         # resolved: item override > item-vendor > vendor
    lot_size: Optional[Decimal] = None
    unit_cost: Optional[Decimal] = None
    last_po_qty: Optional[Decimal] = None
    last_po_date: Optional[dt.date] = None


@dataclass(frozen=True)
class FgUsage:
    """One finished good's contribution to a component (expanded breakdown row)."""
    fg_sku: str
    fg_description: str
    qty_per: Decimal
    fg_demand_per_month: Decimal
    fg_on_hand: Decimal
    component_per_month: Decimal
    demand_in_lead_time: Decimal
    embedded_qty: Decimal


@dataclass
class ComponentPlan:
    component: ComponentInput
    breakdown: list[FgUsage]
    usage_per_month: Decimal
    lead_time_months: Decimal
    demand_in_lead_time: Decimal
    embedded_stock: Decimal
    supply_position: Decimal
    months_of_supply: Optional[Decimal]      # None = infinite (no usage)
    target_months: Decimal
    needed: Decimal
    suggested_qty: Decimal
    estimated_cost: Optional[Decimal]
    order_by_date: Optional[dt.date]         # None = no usage, nothing to plan
    order_now: bool
    status: Status
    flags: set[Flag] = field(default_factory=set)

    @property
    def sku(self) -> str:
        return self.component.sku

    @property
    def needs_order(self) -> bool:
        return self.suggested_qty > 0


@dataclass
class PlanSummary:
    shown: int
    components_to_order: int
    out_before_arrival: list[str]            # SKUs with stockout risk
    next_order_by: Optional[dt.date]
    next_order_now: bool
    late_count: int
    estimated_value: Decimal
    vendor_count: int
    missing_cost_count: int
