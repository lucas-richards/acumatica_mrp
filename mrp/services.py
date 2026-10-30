"""Glue between the database and the pure planning package."""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.utils import timezone

from planning import demand as demand_mod
from planning import mrp
from planning.demand import MonthlyQty
from planning.inputs import PlanInputs, build_plan_inputs
from planning.models import STATUS_RANK, ComponentPlan, Flag, PlanSummary, QtyMode, Status
from planning.reminders import VendorReminder, vendor_reminders

from . import models


@dataclass
class PlanOptions:
    cover_months: int
    qty_mode: QtyMode
    vendor_id: Optional[str] = None


@dataclass
class PlanResult:
    options: PlanOptions
    today: dt.date
    inputs: PlanInputs
    all_plans: list[ComponentPlan]
    plans: list[ComponentPlan]          # after the vendor filter
    summary: PlanSummary
    reminders: list[VendorReminder]
    reminder_days: int
    demand_source: str


def today() -> dt.date:
    return timezone.localdate()


def fg_demand(source: str, window_months: int, on: dt.date):
    """Demand per finished good. Once a source has any data loaded, a finished good with no
    rows (e.g. nothing shipped in the window) has zero demand rather than missing demand."""
    buckets = [MonthlyQty(sku, month, qty)
               for sku, month, qty in models.DemandBucket.objects.filter(source=source)
               .values_list('sku', 'month', 'qty')]
    demand = demand_mod.get_demand_function(source)(buckets, on, window_months)
    if buckets:
        fg_skus = models.StockItem.objects.filter(kind=models.StockItem.FINISHED_GOOD).values_list('sku', flat=True)
        demand = {sku: Decimal(0) for sku in fg_skus} | demand
    return demand


def load_inputs(settings_obj: models.PlanSettings, on: dt.date) -> PlanInputs:
    return build_plan_inputs(
        stock_items=models.StockItem.objects.values(),
        on_hand=models.OnHand.objects.values(),
        open_po_lines=models.OpenPOLine.objects.values(),
        bom=models.BomLine.objects.values(),
        vendors=models.Vendor.objects.values(),
        item_vendors=models.ItemVendor.objects.values(),
        po_history=models.PoHistory.objects.values(),
        fg_demand=fg_demand(settings_obj.demand_source, settings_obj.demand_window_months, on),
        lead_time_overrides=dict(models.LeadTimeOverride.objects.values_list('sku', 'lead_time_days')),
    )


def build_plan(options: PlanOptions, settings_obj: Optional[models.PlanSettings] = None,
               on: Optional[dt.date] = None) -> PlanResult:
    settings_obj = settings_obj or models.PlanSettings.load()
    on = on or today()
    inputs = load_inputs(settings_obj, on)
    all_plans = mrp.plan_components(inputs.components, inputs.bom_lines, inputs.finished_goods,
                                    options.cover_months, options.qty_mode, on)
    # Components replenished by manufacturing (Inventory Planning "Source") are not purchased
    sources = dict(models.StockItem.objects.filter(kind=models.StockItem.COMPONENT)
                   .values_list('sku', 'replenishment_source'))
    all_plans = [p for p in all_plans if sources.get(p.sku) != MANUFACTURING]
    for p in all_plans:
        if sources.get(p.sku) == MIXED:
            p.flags.add(Flag.MIXED_SOURCE)
    plans = [p for p in all_plans if not options.vendor_id or p.component.vendor_id == options.vendor_id]
    return PlanResult(
        options=options,
        today=on,
        inputs=inputs,
        all_plans=all_plans,
        plans=plans,
        summary=mrp.summarize(plans),
        reminders=vendor_reminders(all_plans, inputs.vendors, on, settings_obj.reminder_days),
        reminder_days=settings_obj.reminder_days,
        demand_source=settings_obj.demand_source,
    )


# Orders always cover the lead time plus this many months after the order arrives.
COVER_MONTHS = 2

# Values of StockItem.replenishment_source
MANUFACTURING = 'Manufacturing'
MIXED = 'Mixed'


def default_options(settings_obj: Optional[models.PlanSettings] = None) -> PlanOptions:
    return PlanOptions(cover_months=COVER_MONTHS, qty_mode=QtyMode.COVER_TARGET)


def options_from_query(params, settings_obj: models.PlanSettings) -> PlanOptions:
    """Only the vendor filter comes from the query string; cover and quantity mode are fixed."""
    options = default_options(settings_obj)
    vendor = (params.get('vendor') or '').strip()
    if vendor and models.Vendor.objects.filter(vendor_id=vendor).exists():
        options.vendor_id = vendor
    return options


def last_sync() -> Optional[models.SyncRun]:
    return models.SyncRun.objects.filter(status=models.SyncRun.OK).first()


def purchasing_vendors() -> list[tuple[str, str]]:
    """(vendor_id, name) of vendors that are the planning vendor for at least one component,
    most used first: by PO lines in the synced history, then by number of components supplied."""
    component_skus = set(models.StockItem.objects.filter(kind=models.StockItem.COMPONENT)
                         .exclude(replenishment_source=MANUFACTURING).values_list('sku', flat=True))
    chosen: dict[str, tuple[bool, str]] = {}   # sku -> (is_default, vendor_id), same rule as planning inputs
    for sku, vendor_id, is_default in models.ItemVendor.objects.order_by('id').values_list('sku', 'vendor_id', 'is_default'):
        if sku in component_skus and (sku not in chosen or (is_default and not chosen[sku][0])):
            chosen[sku] = (is_default, vendor_id)
    component_counts = Counter(vendor_id for _, vendor_id in chosen.values())
    po_counts = Counter(models.PoHistory.objects.filter(vendor_id__in=component_counts)
                        .values_list('vendor_id', flat=True))
    names = dict(models.Vendor.objects.filter(vendor_id__in=component_counts).values_list('vendor_id', 'name'))
    ranked = sorted(component_counts, key=lambda v: (-po_counts[v], -component_counts[v], names.get(v, v)))
    return [(v, names.get(v, v)) for v in ranked]


@dataclass
class FgComponent:
    plan: ComponentPlan
    qty_per: Decimal


@dataclass
class FgRow:
    sku: str
    description: str
    demand_per_month: Decimal
    available: Decimal
    components: list[FgComponent]

    @property
    def months_of_stock(self) -> Optional[Decimal]:
        return None if self.demand_per_month <= 0 else self.available / self.demand_per_month

    @property
    def to_order(self) -> list[FgComponent]:
        return [c for c in self.components if c.plan.needs_order]

    @property
    def status(self) -> Status:
        return min((c.plan.status for c in self.components), key=STATUS_RANK.get, default=Status.COVERED)

    @property
    def next_order_by(self) -> Optional[dt.date]:
        dates = [c.plan.order_by_date for c in self.to_order if c.plan.order_by_date]
        return min(dates) if dates else None

    @property
    def order_now(self) -> bool:
        return any(c.plan.order_now for c in self.to_order)


def finished_goods_view(result: PlanResult) -> list[FgRow]:
    """Finished goods with the purchased components they use (vendor filter applied to the
    components), most urgent first. Subassemblies are left out: their components already
    appear under the finished goods that use them."""
    item_types = dict(models.StockItem.objects.filter(kind=models.StockItem.FINISHED_GOOD)
                      .values_list('sku', 'item_type'))
    by_fg: dict[str, list[FgComponent]] = {}
    for plan in result.plans:
        for usage in plan.breakdown:
            by_fg.setdefault(usage.fg_sku, []).append(FgComponent(plan, usage.qty_per))
    rows = []
    for fg in result.inputs.finished_goods:
        if fg.sku not in by_fg or item_types.get(fg.sku) == 'Subassembly':
            continue
        components = sorted(by_fg[fg.sku], key=lambda c: mrp.urgency_key(c.plan))
        rows.append(FgRow(fg.sku, fg.description, fg.demand_per_month or Decimal(0), fg.on_hand, components))
    rows.sort(key=lambda r: (STATUS_RANK[r.status], r.next_order_by or dt.date.max, -r.demand_per_month, r.sku))
    return rows
