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


# A finished good with this many months of stock or fewer is shown as low
LOW_FG_MONTHS = Decimal(1)

# Sort order for urgency (lower = more urgent)
STATUS_RANK = {Status.STOCKOUT_RISK: 0, Status.ORDER_SOON: 1, Status.COVERED: 2}


# Which monthly usage drives an item's plan
BASIS_SIX, BASIS_TWO_YEAR, BASIS_CUSTOM = 'six', 'two_year', 'custom'
BASES = (BASIS_SIX, BASIS_TWO_YEAR, BASIS_CUSTOM)


class Flag(str, Enum):
    NO_USAGE = 'no_usage'                    # no issue history and nothing on order that needs it
    NO_BOM = 'no_bom'                        # not a component of any finished good
    MISSING_LEAD_TIME = 'missing_lead_time'  # no lead time from override, item-vendor or vendor
    NO_VENDOR = 'no_vendor'
    NO_PO_HISTORY = 'no_po_history'          # "Match last PO" requested but no previous PO
    MIXED_SOURCE = 'mixed_source'            # item is set to both purchase and manufacture
    PO_LATE = 'po_late'                      # an open PO line is past its promised date
    NO_DEFAULT_VENDOR = 'no_default_vendor'  # the item has vendors but none is marked default
    VENDOR_MISMATCH = 'vendor_mismatch'      # the last PO went to a vendor other than the default


FLAG_LABELS = {
    Flag.NO_USAGE: 'No usage',
    Flag.NO_BOM: 'Not in any BOM',
    Flag.MISSING_LEAD_TIME: 'Lead time missing',
    Flag.NO_VENDOR: 'No vendor',
    Flag.NO_PO_HISTORY: 'No PO history',
    Flag.MIXED_SOURCE: 'Purchase or manufacture',
    Flag.PO_LATE: 'PO late',
    Flag.NO_DEFAULT_VENDOR: 'No default vendor',
    Flag.VENDOR_MISMATCH: 'Last PO vendor differs from default',
}


@dataclass(frozen=True)
class FinishedGood:
    sku: str
    description: str = ''
    on_hand: Decimal = Decimal('0')              # available (or on hand) finished-good stock
    demand_per_month: Optional[Decimal] = None   # None = no demand data
    last_run_qty: Optional[Decimal] = None       # quantity of the last completed production run
    last_run_date: Optional[dt.date] = None
    unproduced_orders: Decimal = Decimal('0')    # open sales order qty with no production order behind it


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
    in_receiving: Decimal = Decimal('0')         # received, waiting in the receiving location (not in available)
    lead_time_days: Optional[int] = None         # resolved: item override > item-vendor > vendor
    lot_size: Optional[Decimal] = None
    last_po_qty: Optional[Decimal] = None
    last_po_date: Optional[dt.date] = None
    on_order_since: Optional[dt.date] = None     # order date of the oldest open PO line
    earliest_promised: Optional[dt.date] = None  # earliest promised date among open PO lines
    issue_demand_per_month: Optional[Decimal] = None   # monthly usage from history (issues; for a purchased finished good, invoices + kit breakdowns instead); None = no history
    is_finished_good: bool = False               # a bought finished good, not a component
    long_term_usage: Optional[Decimal] = None    # average per month over the last 24 months, same measure as usage
    usage_basis: str = 'six'                     # which usage drives the plan: 'six', 'two_year' or 'custom'
    custom_usage: Optional[Decimal] = None       # a usage the planner typed in
    has_vendor_rows: bool = False                # the item lists at least one vendor
    default_vendor_id: Optional[str] = None      # the vendor marked default in Acumatica; None when none is
    default_vendor_name: Optional[str] = None
    last_po_vendor_id: Optional[str] = None      # who the most recent PO (any vendor) went to
    last_po_vendor_name: Optional[str] = None
    acked_warnings: frozenset = frozenset()      # vendor warnings marked done within the last week
    active_months: int = 0                       # of those 24 months, how many had any use


@dataclass(frozen=True)
class FgUsage:
    """One finished good's contribution to a component (expanded breakdown row)."""
    fg_sku: str
    fg_description: str
    qty_per: Decimal
    fg_demand_per_month: Optional[Decimal]
    fg_on_hand: Decimal
    last_run_qty: Optional[Decimal]
    last_run_date: Optional[dt.date]
    fg_unproduced_orders: Decimal    # open sales orders of the finished good with no production order
    component_qty: Decimal           # component needed by those orders

    @property
    def months_of_stock(self) -> Optional[Decimal]:
        """Months the finished-good stock lasts at its monthly demand; None without demand."""
        if not self.fg_demand_per_month or self.fg_demand_per_month <= 0:
            return None
        return self.fg_on_hand / self.fg_demand_per_month

    @property
    def is_low(self) -> bool:
        months = self.months_of_stock
        return months is not None and months <= LOW_FG_MONTHS

    @property
    def last_run_component_qty(self) -> Optional[Decimal]:
        """Component used up if the finished good is produced again in the size of its last run."""
        return None if self.last_run_qty is None else self.last_run_qty * self.qty_per


@dataclass
class ComponentPlan:
    component: ComponentInput
    breakdown: list[FgUsage]
    usage_per_month: Decimal
    usage_basis: str                         # the basis that was actually used (see BASES)
    lead_time_months: Decimal
    demand_in_lead_time: Decimal
    committed: Decimal                       # needed by open orders that have no production order
    supply_position: Decimal
    months_of_supply: Optional[Decimal]      # None = infinite (no usage)
    target_months: Decimal
    needed: Decimal
    suggested_qty: Decimal
    order_by_date: Optional[dt.date]         # None = no usage, nothing to plan
    order_now: bool
    status: Status
    flags: set[Flag] = field(default_factory=set)

    @property
    def sku(self) -> str:
        return self.component.sku

    @property
    def prepare_days(self) -> int:
        """Days between the lead time (red line) and the reorder point (black line)."""
        return int(((self.target_months - self.lead_time_months) * 30).to_integral_value())

    @property
    def default_order_qty(self) -> Decimal:
        """What the order box starts with: always 0. The planner types what to order; the suggestion and the
        last order are shown next to it for reference."""
        return Decimal(0)

    @property
    def needs_order(self) -> bool:
        return self.suggested_qty > 0

    @property
    def on_hand_and_order(self) -> Decimal:
        return self.component.on_hand + self.component.on_order + self.component.in_receiving

    @property
    def months_at_arrival(self) -> Optional[Decimal]:
        """Months of inventory left when an order placed today would arrive (after the lead time):
        (available + on order + in Receiving - lead time x monthly usage) / monthly usage, without the
        new order. Negative means stock runs out that many months before it could arrive. None when
        there is no usage."""
        return self.months_at_arrival_with(Decimal(0))

    def months_at_arrival_with(self, order_qty: Decimal) -> Optional[Decimal]:
        """The same, counting an order of this size as arriving: (available + on order + in Receiving +
        order qty - lead time x monthly usage) / monthly usage. None when the lead time or the usage
        is missing (see missing_for_months_left)."""
        if self.missing_for_months_left:
            return None
        return (self.stock_for_arrival + order_qty - self.usage_per_month * self.lead_time_months) / self.usage_per_month

    @property
    def missing_for_months_left(self) -> str:
        """What the months-left figures cannot be calculated without: 'lead time', 'demand', both, or ''."""
        no_lead = self.component.lead_time_days is None
        no_demand = self.usage_per_month <= 0
        if no_lead and no_demand:
            return 'lead time and demand'
        return 'lead time' if no_lead else ('demand' if no_demand else '')

    @property
    def stock_for_arrival(self) -> Decimal:
        """What will be on hand when an order arrives, before the lead time's usage: available + on order
        + in Receiving. Open orders without a production order are not taken off here."""
        c = self.component
        return c.on_hand + c.on_order + c.in_receiving

    @property
    def months_at_arrival_with_default(self) -> Optional[Decimal]:
        return self.months_at_arrival_with(self.default_order_qty)


@dataclass
class PlanSummary:
    shown: int
    components_to_order: int
    out_before_arrival: list[str]            # SKUs with stockout risk
    next_order_by: Optional[dt.date]
    next_order_now: bool
    late_count: int
    vendor_count: int
