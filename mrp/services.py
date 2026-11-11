"""Glue between the database and the pure planning package."""
from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.db.models import Count, Max
from django.utils import timezone

from planning import demand as demand_mod
from planning import mrp
from planning.demand import ASSEMBLY_SOURCE, ISSUE_SOURCE, OPEN_PO_SOURCE, OPEN_SO_SOURCE, MonthlyQty
from planning.inputs import PlanInputs, build_plan_inputs
from planning.models import BASIS_CUSTOM, BASIS_SIX, STATUS_RANK, ComponentPlan, Flag, PlanSummary, QtyMode, Status
from planning.reminders import VendorReminder, vendor_reminders

from . import models


@dataclass
class PlanOptions:
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


def _source_demand(source: str, window_months: int, on: dt.date) -> tuple[dict[str, Decimal], bool]:
    """(average per month by sku, whether the source has any rows loaded)."""
    buckets = [MonthlyQty(sku, month, qty)
               for sku, month, qty in models.DemandBucket.objects.filter(source=source)
               .values_list('sku', 'month', 'qty')]
    return demand_mod.get_demand_function(source)(buckets, on, window_months), bool(buckets)


def _add(a: dict[str, Decimal], b: dict[str, Decimal]) -> dict[str, Decimal]:
    return {sku: a.get(sku, Decimal(0)) + b.get(sku, Decimal(0)) for sku in a.keys() | b.keys()}


def fg_demand(source: str, window_months: int, on: dt.date):
    """Demand per finished good. Once a source has any data loaded, a finished good with no
    rows (e.g. nothing invoiced in the window) has zero demand rather than missing demand.
    With invoice history, units consumed by kit assemblies ("assembly out") are added."""
    demand, loaded = _source_demand(source, window_months, on)
    if source == 'trailing_invoices':
        demand = _add(demand, _source_demand(ASSEMBLY_SOURCE, window_months, on)[0])
    if loaded:
        fg_skus = models.StockItem.objects.filter(kind=models.StockItem.FINISHED_GOOD).values_list('sku', flat=True)
        demand = {sku: Decimal(0) for sku in fg_skus} | demand
    return demand


def issue_demand(window_months: int, on: dt.date) -> dict[str, Decimal]:
    """Trailing average monthly issues plus assembly out per component. Only components with
    rows are present; the planner falls back to finished-good demand x BOM for the rest."""
    return _add(_source_demand(ISSUE_SOURCE, window_months, on)[0],
                _source_demand(ASSEMBLY_SOURCE, window_months, on)[0])


HISTORY_MONTHS = 24


def usage_histories(on: dt.date) -> tuple[dict, dict]:
    """(components, bought finished goods): average per month and months with use over the last two
    years, measured the same way as the usage that drives the plan."""
    def history(sources):
        buckets = [MonthlyQty(sku, month, qty) for sku, month, qty in models.DemandBucket.objects
                   .filter(source__in=sources).values_list('sku', 'month', 'qty')]
        return demand_mod.usage_history(buckets, on, HISTORY_MONTHS)
    return history([ISSUE_SOURCE, ASSEMBLY_SOURCE]), history(['trailing_invoices', ASSEMBLY_SOURCE])


def unproduced_orders() -> dict[str, Decimal]:
    """Open sales order quantity per finished good that has no production order behind it:
    open sales orders less the quantity still to be produced, never below zero."""
    totals: dict[str, Decimal] = {}
    for source, sign in ((OPEN_SO_SOURCE, 1), (OPEN_PO_SOURCE, -1)):
        for sku, qty in models.DemandBucket.objects.filter(source=source).values_list('sku', 'qty'):
            totals[sku] = totals.get(sku, Decimal(0)) + sign * qty
    return {sku: qty for sku, qty in totals.items() if qty > 0}


def load_inputs(settings_obj: models.PlanSettings, on: dt.date) -> PlanInputs:
    usage_choices = {c.sku: (c.basis, c.custom_usage) for c in models.UsageChoice.objects.all()}
    acks = warning_acks(on)
    issue_history, fg_history = usage_histories(on)
    return build_plan_inputs(
        stock_items=models.StockItem.objects.values(),
        on_hand=models.OnHand.objects.values(),
        open_po_lines=models.OpenPOLine.objects.values(),
        bom=models.BomLine.objects.values(),
        vendors=models.Vendor.objects.values(),
        item_vendors=models.ItemVendor.objects.values(),
        po_history=models.PoHistory.objects.values(),
        fg_demand=fg_demand(settings_obj.demand_source, settings_obj.demand_window_months, on),
        issue_demand=issue_demand(settings_obj.demand_window_months, on),
        unproduced_orders=unproduced_orders(),
        issue_history=issue_history,
        usage_choices=usage_choices,
        warning_acks=acks,
        fg_history=fg_history,
        last_production={sku: (qty, run_date) for sku, qty, run_date
                         in models.LastProduction.objects.values_list('sku', 'qty', 'run_date')},
    )


# The plan only changes when new data is loaded (the morning sync, "Refresh from Acumatica", an import),
# when a lead-time override or plan setting changes, or when the day rolls over. Until then the computed
# plan is reused, so switching vendors or coming back from another page does not rebuild it. Each
# process keeps the latest one; the key is read from a few small queries, so a refresh in another
# process is noticed straight away.
_cache: dict = {}


def _data_key(settings_obj: models.PlanSettings, on: dt.date) -> tuple:
    sync_run = last_sync()
    return (sync_run.id if sync_run else None, sync_run.finished_at if sync_run else None,
            settings_obj.updated_at, on)


ACK_DAYS = 7          # a vendor warning marked done stays hidden this long


def _choice_key() -> tuple:
    """Changes whenever an item's usage choice or a warning's done mark changes."""
    choices = models.UsageChoice.objects.aggregate(n=Count('id'), latest=Max('updated_at'))
    acks = models.WarningAck.objects.aggregate(n=Count('id'), latest=Max('updated_at'))
    return choices['n'], choices['latest'], acks['n'], acks['latest']


def warning_acks(on: dt.date) -> dict[str, frozenset]:
    """Per item, the warnings marked done within the last week (those are hidden for now)."""
    acks: dict[str, set] = {}
    for sku, flag, acked_on in models.WarningAck.objects.values_list('sku', 'flag', 'acked_on'):
        if (on - acked_on).days < ACK_DAYS:
            acks.setdefault(sku, set()).add(flag)
    return {sku: frozenset(flags) for sku, flags in acks.items()}


def clear_cache() -> None:
    _cache.clear()


def _cached(name: str, key: tuple, build):
    entry = _cache.get(name)
    if entry is None or entry[0] != key:
        entry = _cache[name] = (key, build())
    return entry[1]


def _build_base(settings_obj: models.PlanSettings, on: dt.date, qty_mode: QtyMode):
    inputs = load_inputs(settings_obj, on)
    all_plans = mrp.plan_components(inputs.components, inputs.bom_lines, inputs.finished_goods, qty_mode, on)
    # Components replenished by manufacturing (Inventory Planning "Source") are not purchased
    sources = dict(models.StockItem.objects.values_list('sku', 'replenishment_source'))
    all_plans = [p for p in all_plans if sources.get(p.sku) not in MADE_IN_HOUSE]
    # Items whose planning method is None are not planned, so not listed
    unplanned = set(models.StockItem.objects.filter(planning_method=NO_PLANNING).values_list('sku', flat=True))
    all_plans = [p for p in all_plans if p.sku not in unplanned]
    for p in all_plans:
        if sources.get(p.sku) == MIXED:
            p.flags.add(Flag.MIXED_SOURCE)
    # Bought items with no usage and nothing on order are not worth listing
    all_plans = [p for p in all_plans if not (p.component.is_finished_good and p.usage_per_month <= 0
                                              and p.component.on_order <= 0)]
    reminders = vendor_reminders(all_plans, inputs.vendors, on, settings_obj.reminder_days)
    return inputs, all_plans, reminders


def build_plan(options: PlanOptions, settings_obj: Optional[models.PlanSettings] = None,
               on: Optional[dt.date] = None) -> PlanResult:
    settings_obj = settings_obj or models.PlanSettings.load()
    on = on or today()
    key = _data_key(settings_obj, on) + (options.qty_mode, _choice_key())
    inputs, all_plans, reminders = _cached('plan', key, lambda: _build_base(settings_obj, on, options.qty_mode))
    plans = [p for p in all_plans if not options.vendor_id or p.component.vendor_id == options.vendor_id]
    return PlanResult(
        options=options,
        today=on,
        inputs=inputs,
        all_plans=all_plans,
        plans=plans,
        summary=mrp.summarize(plans),
        reminders=reminders,
        reminder_days=settings_obj.reminder_days,
        demand_source=settings_obj.demand_source,
    )


# Values of StockItem.replenishment_source
MANUFACTURING = 'Manufacturing'
KIT_ASSEMBLY = 'Kit Assembly'
NO_PLANNING = 'None'     # StockItem.planning_method of items that are not planned
MADE_IN_HOUSE = (MANUFACTURING, KIT_ASSEMBLY)   # planned on the production side, not purchased
PURCHASE = 'Purchase'
MIXED = 'Mixed'


def default_options(settings_obj: Optional[models.PlanSettings] = None) -> PlanOptions:
    return PlanOptions(qty_mode=QtyMode.COVER_TARGET)


def options_from_query(params, settings_obj: models.PlanSettings) -> PlanOptions:
    """Only the vendor filter comes from the query string; cover and quantity mode are fixed."""
    options = default_options(settings_obj)
    vendor = (params.get('vendor') or '').strip()
    if vendor and models.Vendor.objects.filter(vendor_id=vendor).exists():
        options.vendor_id = vendor
    return options


def last_sync() -> Optional[models.SyncRun]:
    return models.SyncRun.objects.filter(status=models.SyncRun.OK).first()


_UNSET = object()


def _replan_item(sku: str, before, **changes) -> None:
    """Plan one item again inside the cached plan, with the given changes to its inputs, so a click updates
    the page without rebuilding every item. `before` is what _begin_change returned ahead of the change."""
    settings_obj, on, base_key, entry, cached_before = before
    if not cached_before:
        return                        # nothing cached (or it was already stale): the next page load rebuilds
    inputs, all_plans, _reminders = entry[1]
    index = next((n for n, plan in enumerate(all_plans) if plan.sku == sku), None)
    position = next((n for n, c in enumerate(inputs.components) if c.sku == sku), None)
    if index is None or position is None:
        return
    component = dataclasses.replace(inputs.components[position], **changes)
    inputs.components[position] = component
    replanned = mrp.plan_component(
        component, [line for line in inputs.bom_lines if line.component_sku == sku],
        {fg.sku: fg for fg in inputs.finished_goods}, QtyMode.COVER_TARGET, on)
    replanned.flags |= all_plans[index].flags & {Flag.MIXED_SOURCE}
    all_plans[index] = replanned
    reminders = vendor_reminders(all_plans, inputs.vendors, on, settings_obj.reminder_days)
    _cache['plan'] = (base_key + (_choice_key(),), (inputs, all_plans, reminders))


def _begin_change():
    settings_obj = models.PlanSettings.load()
    on = today()
    base_key = _data_key(settings_obj, on) + (QtyMode.COVER_TARGET,)
    entry = _cache.get('plan')
    return settings_obj, on, base_key, entry, entry is not None and entry[0] == base_key + (_choice_key(),)


def set_usage_choice(sku: str, user, basis: Optional[str] = None, custom=_UNSET) -> None:
    """Choose which monthly usage drives an item's plan, and/or set its custom usage.

    Typing a custom usage selects it; clearing it goes back to the recent usage if it was selected."""
    before = _begin_change()
    choice, _ = models.UsageChoice.objects.get_or_create(sku=sku)
    if custom is not _UNSET:
        choice.custom_usage = custom
        if custom is not None and basis is None:
            basis = BASIS_CUSTOM
        elif custom is None and choice.basis == BASIS_CUSTOM and basis is None:
            basis = BASIS_SIX
    if basis is not None:
        choice.basis = basis
    choice.updated_by = user if getattr(user, 'is_authenticated', False) else None
    choice.save()
    _replan_item(sku, before, usage_basis=choice.basis, custom_usage=choice.custom_usage)


VENDOR_WARNINGS = (Flag.NO_DEFAULT_VENDOR.value, Flag.VENDOR_MISMATCH.value)


def acknowledge_warning(sku: str, flag: str, user) -> None:
    """Mark a vendor warning on an item as done: it is hidden for a week and shows again if still not fixed."""
    before = _begin_change()
    settings_obj, on = before[0], before[1]
    models.WarningAck.objects.update_or_create(
        sku=sku, flag=flag,
        defaults={'acked_on': on, 'acked_by': user if getattr(user, 'is_authenticated', False) else None})
    entry = before[3]
    current = frozenset()
    if entry is not None:
        current = next((c.acked_warnings for c in entry[1][0].components if c.sku == sku), frozenset())
    _replan_item(sku, before, acked_warnings=current | {flag})
