import datetime as dt
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from planning.models import BASES

from . import exports, models, services, sync
from .roles import is_planner, planner_required, viewer_required

def _query(options, **changes):
    params = {'vendor': options.vendor_id or ''}
    params.update(changes)
    params = {k: v for k, v in params.items() if v not in ('', None)}
    return '?' + urlencode(params) if params else ''


def _plan_context(request):
    settings_obj = models.PlanSettings.load()
    options = services.options_from_query(request.GET, settings_obj)
    result = services.build_plan(options, settings_obj)
    # Vendors that have something to order, with how many items, most first
    names, counts = {}, {}
    for p in result.all_plans:
        vendor_id = p.component.vendor_id
        if vendor_id and p.needs_order:
            names[vendor_id] = p.component.vendor_name or vendor_id
            counts[vendor_id] = counts.get(vendor_id, 0) + 1
    if options.vendor_id and options.vendor_id not in names:
        # Keep the vendor you picked visible even when nothing is due for it
        names[options.vendor_id] = next((p.component.vendor_name for p in result.all_plans
                                         if p.component.vendor_id == options.vendor_id), options.vendor_id)
        counts[options.vendor_id] = 0
    ranked = sorted(names, key=lambda v: (-counts[v], names[v].lower()))
    return {
        'nav': 'plan',
        'result': result,
        'settings': settings_obj,
        'options': options,
        'is_planner': is_planner(request.user),
        'vendor_chips': [('', 'All vendors', None, _query(options, vendor=''))]
                        + [(v, names[v], counts[v], _query(options, vendor=v)) for v in ranked],
        'query': _query(options),
        'history_url': settings.INVENTORY_HISTORY_URL,
        'last_sync': services.last_sync(),
        'acumatica_configured': sync.acumatica_configured(),
        'running_sync': sync.running_sync(),
    }


@require_GET
@viewer_required
def plan(request):
    context = _plan_context(request)
    # HTMX filter clicks swap only the plan content
    template = 'mrp/_plan_content.html' if request.headers.get('HX-Request') and not request.headers.get('HX-History-Restore-Request') else 'mrp/plan.html'
    return render(request, template, context)


@require_GET
@viewer_required
def breakdown(request, sku):
    settings_obj = models.PlanSettings.load()
    options = services.options_from_query(request.GET, settings_obj)
    result = services.build_plan(options, settings_obj)
    plan_row = next((p for p in result.all_plans if p.sku == sku), None)
    if plan_row is None:
        raise Http404('Unknown component')
    return render(request, 'mrp/_breakdown.html', {'plan': plan_row})


@require_GET
@viewer_required
def report(request, sku):
    """The purchase requisition sheet for one item (an A4 page, shown in a pop-up and printed from there)."""
    settings_obj = models.PlanSettings.load()
    options = services.options_from_query(request.GET, settings_obj)
    result = services.build_plan(options, settings_obj)
    plan_row = next((p for p in result.all_plans if p.sku == sku), None)
    if plan_row is None:
        raise Http404('Unknown component')
    try:
        qty = max(Decimal(request.GET.get('qty', '0').replace(',', '').strip() or '0'), Decimal(0))
    except InvalidOperation:
        qty = Decimal(0)
    today = timezone.localdate()
    lead = plan_row.component.lead_time_days
    user = request.user
    return render(request, 'mrp/_report.html', {
        'p': plan_row,
        'qty': qty,
        'with_order': plan_row.months_at_arrival_with(qty),
        'today': today,
        'needed_by': today + dt.timedelta(days=lead or 0),
        'requested_by': (user.get_full_name() or user.get_username()) if user.is_authenticated else '',
        'history_url': settings.INVENTORY_HISTORY_URL,
    })


@require_POST
@planner_required
def set_usage(request):
    """HTMX: choose which monthly usage drives one item (basis) and/or set its custom usage, then give
    back that item's row."""
    sku = request.POST.get('sku', '')
    basis = request.POST.get('basis') or None
    if basis is not None and basis not in BASES:
        return HttpResponse('Unknown usage', status=400)
    options = services.default_options()
    if not next((p for p in services.build_plan(options).all_plans if p.sku == sku), None):
        raise Http404('Unknown item')
    custom = services._UNSET
    if 'custom' in request.POST:
        text = request.POST.get('custom', '').replace(',', '').strip()
        try:
            custom = Decimal(text) if text else None
            if custom is not None and (not custom.is_finite() or custom < 0):
                raise InvalidOperation
        except InvalidOperation:
            custom = services._UNSET          # not a number: leave it as it was
    services.set_usage_choice(sku, request.user, basis=basis, custom=custom)
    result = services.build_plan(options)
    plan_row = next(p for p in result.all_plans if p.sku == sku)
    return render(request, 'mrp/_plan_row.html', {
        'p': plan_row, 'is_planner': True, 'settings': models.PlanSettings.load(), 'query': '',
        'stock_label': 'Available' if result.inputs.uses_available else 'On hand'})


@require_POST
@planner_required
def warning_done(request):
    """HTMX: mark a vendor warning on one item as done (hidden for a week), then give back its row."""
    sku = request.POST.get('sku', '')
    flag = request.POST.get('flag', '')
    if flag not in services.VENDOR_WARNINGS:
        return HttpResponse('Unknown warning', status=400)
    options = services.default_options()
    if not next((p for p in services.build_plan(options).all_plans if p.sku == sku), None):
        raise Http404('Unknown item')
    services.acknowledge_warning(sku, flag, request.user)
    result = services.build_plan(options)
    plan_row = next(p for p in result.all_plans if p.sku == sku)
    return render(request, 'mrp/_plan_row.html', {
        'p': plan_row, 'is_planner': True, 'settings': models.PlanSettings.load(), 'query': '',
        'stock_label': 'Available' if result.inputs.uses_available else 'On hand'})


@require_GET
@viewer_required
def export_csv(request):
    settings_obj = models.PlanSettings.load()
    options = services.options_from_query(request.GET, settings_obj)
    result = services.build_plan(options, settings_obj)
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="purchase-plan-{result.today:%Y-%m-%d}.csv"'
    exports.write_plan_csv(response, result)
    return response


def _back(request):
    target = request.POST.get('next') or reverse('mrp:plan')
    if not target.startswith('/') or target.startswith('//'):
        target = reverse('mrp:plan')
    return redirect(target)


@require_POST
@planner_required
def refresh(request):
    if not sync.acumatica_configured():
        messages.error(request, 'Acumatica is not connected yet. The planner is showing the sample or imported data.')
        return _back(request)
    try:
        sync.start_background_sync(request.user)
    except sync.SyncInProgress as exc:
        messages.warning(request, str(exc))
    return _back(request)


@require_GET
@viewer_required
def refresh_status(request):
    """HTMX poll target: the refresh banner while a sync runs; tells the page to reload when done."""
    running = sync.running_sync()
    if running:
        return render(request, 'mrp/_refresh_status.html', {'running': running})
    latest = models.SyncRun.objects.filter(source=models.SyncRun.ACUMATICA).first()
    if latest and latest.status == models.SyncRun.OK:
        messages.success(request, f'Refreshed from Acumatica ({sum(latest.counts.values()):,} rows'
                                  f'{", " + str(len(latest.warnings)) + " warnings" if latest.warnings else ""}).')
    elif latest:
        messages.error(request, f'Refresh failed; the previous data is still shown. {latest.error}')
    response = HttpResponse('')
    response['HX-Refresh'] = 'true'
    return response
