"""Glue between the database and the pure planning package."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

from django.utils import timezone

from planning import demand as demand_mod
from planning import mrp
from planning.demand import MonthlyQty
from planning.inputs import PlanInputs, build_plan_inputs
from planning.models import ComponentPlan, PlanSummary, QtyMode
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
    buckets = [MonthlyQty(sku, month, qty)
               for sku, month, qty in models.DemandBucket.objects.filter(source=source)
               .values_list('sku', 'month', 'qty')]
    return demand_mod.get_demand_function(source)(buckets, on, window_months)


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


def default_options(settings_obj: Optional[models.PlanSettings] = None) -> PlanOptions:
    settings_obj = settings_obj or models.PlanSettings.load()
    return PlanOptions(cover_months=settings_obj.cover_months, qty_mode=QtyMode(settings_obj.qty_mode))


def options_from_query(params, settings_obj: models.PlanSettings) -> PlanOptions:
    """Read vendor/cover/mode from GET params, falling back to saved defaults on bad input."""
    options = default_options(settings_obj)
    try:
        cover = int(params.get('cover', options.cover_months))
        if 0 <= cover <= mrp.MAX_COVER_MONTHS:
            options.cover_months = cover
    except (TypeError, ValueError):
        pass
    try:
        options.qty_mode = QtyMode(params.get('mode', options.qty_mode.value))
    except ValueError:
        pass
    vendor = (params.get('vendor') or '').strip()
    if vendor and models.Vendor.objects.filter(vendor_id=vendor).exists():
        options.vendor_id = vendor
    return options


def last_sync() -> Optional[models.SyncRun]:
    return models.SyncRun.objects.filter(status=models.SyncRun.OK).first()
