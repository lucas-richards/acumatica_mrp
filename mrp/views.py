from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from . import exports, models, services, sync
from .forms import LeadTimeOverrideForm
from .roles import is_planner, planner_required, viewer_required

TOP_VENDOR_CHIPS = 5
VENDOR_SEARCH_LIMIT = 12


def _query(options, **changes):
    params = {'vendor': options.vendor_id or ''}
    params.update(changes)
    params = {k: v for k, v in params.items() if v not in ('', None)}
    return '?' + urlencode(params) if params else ''


def _plan_context(request):
    settings_obj = models.PlanSettings.load()
    options = services.options_from_query(request.GET, settings_obj)
    result = services.build_plan(options, settings_obj)
    vendors = services.purchasing_vendors()
    top = vendors[:TOP_VENDOR_CHIPS]
    if options.vendor_id and options.vendor_id not in dict(top):
        # Keep a vendor picked from search visible as the active chip
        top.append((options.vendor_id, dict(vendors).get(options.vendor_id, options.vendor_id)))
    return {
        'nav': 'plan',
        'result': result,
        'settings': settings_obj,
        'options': options,
        'is_planner': is_planner(request.user),
        'vendor_chips': [('', 'All vendors', _query(options, vendor=''))]
                        + [(vid, name, _query(options, vendor=vid)) for vid, name in top],
        'more_vendors': len(vendors) - min(len(vendors), TOP_VENDOR_CHIPS),
        'query': _query(options),
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
def vendor_search(request):
    """HTMX: vendor chips matching the search text (name or ID), keeping cover and mode."""
    options = services.options_from_query(request.GET, models.PlanSettings.load())
    q = (request.GET.get('q') or '').strip().lower()
    matches = []
    if q:
        matches = [(vid, name) for vid, name in services.purchasing_vendors()
                   if q in name.lower() or q in vid.lower()][:VENDOR_SEARCH_LIMIT]
    return render(request, 'mrp/_vendor_results.html', {
        'q': q,
        'results': [(vid, name, _query(options, vendor=vid)) for vid, name in matches],
        'limit': VENDOR_SEARCH_LIMIT,
    })


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


@viewer_required
def lead_times(request):
    planner = is_planner(request.user)
    form = LeadTimeOverrideForm(request.POST or None)
    if request.method == 'POST':
        if not planner:
            raise PermissionDenied('Only planners can change lead times.')
        if form.is_valid():
            sku = form.cleaned_data['sku']
            days = form.cleaned_data['lead_time_days']
            existing = models.LeadTimeOverride.objects.filter(sku=sku).first()
            models.SettingsChangeLog.record(request.user, f'lead_time_override:{sku}',
                                            existing.lead_time_days if existing else '', days)
            models.LeadTimeOverride.objects.update_or_create(
                sku=sku, defaults={'lead_time_days': days, 'note': form.cleaned_data['note'],
                                   'updated_by': request.user})
            messages.success(request, f'Lead time for {sku} set to {days} days.')
            return redirect('mrp:lead_times')
    result = services.build_plan(services.default_options())
    return render(request, 'mrp/lead_times.html', {
        'nav': 'lead_times',
        'form': form,
        'is_planner': planner,
        'overrides': models.LeadTimeOverride.objects.select_related('updated_by'),
        'components': result.inputs.components,
        'lead_time_sources': result.inputs.lead_time_sources,
        'changes': models.SettingsChangeLog.objects.select_related('user')[:50],
    })


@require_POST
@planner_required
def delete_lead_time(request, sku):
    override = models.LeadTimeOverride.objects.filter(sku=sku).first()
    if override:
        models.SettingsChangeLog.record(request.user, f'lead_time_override:{sku}', override.lead_time_days, '')
        override.delete()
        messages.success(request, f'Override for {sku} removed; using the vendor lead time again.')
    return redirect('mrp:lead_times')
