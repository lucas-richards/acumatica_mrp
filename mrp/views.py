from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from acumatica.rest import AcumaticaError
from planning.mrp import MAX_COVER_MONTHS
from planning.models import QtyMode
from planning.reminders import REMINDER_DAY_CHOICES

from . import exports, loaders, models, services, sync
from .forms import LeadTimeOverrideForm
from .roles import is_planner, planner_required, viewer_required

COVER_CHOICES = [(0, 'Lead time only')] + [(n, f'+{n} month' + ('s' if n > 1 else '')) for n in range(1, MAX_COVER_MONTHS + 1)]
QTY_MODE_CHOICES = models.PlanSettings.QTY_MODE_CHOICES


def _query(options, **changes):
    params = {'vendor': options.vendor_id or '', 'cover': options.cover_months, 'mode': options.qty_mode.value}
    params.update(changes)
    return '?' + urlencode({k: v for k, v in params.items() if v not in ('', None)})


def _plan_context(request):
    settings_obj = models.PlanSettings.load()
    options = services.options_from_query(request.GET, settings_obj)
    result = services.build_plan(options, settings_obj)
    vendors = list(models.Vendor.objects.values_list('vendor_id', 'name'))
    return {
        'nav': 'plan',
        'result': result,
        'settings': settings_obj,
        'options': options,
        'is_planner': is_planner(request.user),
        'is_default_view': (options.cover_months == settings_obj.cover_months
                            and options.qty_mode.value == settings_obj.qty_mode),
        'vendor_chips': [('', 'All vendors', _query(options, vendor=''))]
                        + [(vid, name, _query(options, vendor=vid)) for vid, name in vendors],
        'cover_chips': [(n, label, _query(options, cover=n)) for n, label in COVER_CHOICES],
        'mode_chips': [(value, label, _query(options, mode=value)) for value, label in QTY_MODE_CHOICES],
        'reminder_choices': REMINDER_DAY_CHOICES,
        'query': _query(options),
        'last_sync': services.last_sync(),
        'acumatica_configured': sync.acumatica_configured(),
        'subscribed': models.WeeklyDigestSubscription.objects.filter(user=request.user).exists(),
        'cover_target_mode': options.qty_mode == QtyMode.COVER_TARGET,
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
def export_csv(request):
    settings_obj = models.PlanSettings.load()
    options = services.options_from_query(request.GET, settings_obj)
    result = services.build_plan(options, settings_obj)
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="purchase-plan-{result.today:%Y-%m-%d}.csv"'
    exports.write_plan_csv(response, result)
    return response


@require_GET
@viewer_required
def calendar_ics(request):
    result = services.build_plan(services.default_options())
    response = HttpResponse(exports.order_by_calendar(result, request.build_absolute_uri(reverse('mrp:plan'))),
                            content_type='text/calendar; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="order-by-dates.ics"'
    return response


def _back(request):
    target = request.POST.get('next') or reverse('mrp:plan')
    if not target.startswith('/') or target.startswith('//'):
        target = reverse('mrp:plan')
    return redirect(target)


@require_POST
@planner_required
def save_defaults(request):
    settings_obj = models.PlanSettings.load()
    try:
        cover = int(request.POST.get('cover', ''))
        mode = QtyMode(request.POST.get('mode', ''))
    except ValueError:
        messages.error(request, 'Invalid planning defaults.')
        return _back(request)
    if not 0 <= cover <= MAX_COVER_MONTHS:
        messages.error(request, 'Cover months must be between 0 and 4.')
        return _back(request)
    models.SettingsChangeLog.record(request.user, 'cover_months', settings_obj.cover_months, cover)
    models.SettingsChangeLog.record(request.user, 'qty_mode', settings_obj.qty_mode, mode.value)
    settings_obj.cover_months = cover
    settings_obj.qty_mode = mode.value
    settings_obj.save()
    messages.success(request, 'Saved as the default view and for reminder emails.')
    return _back(request)


@require_POST
@planner_required
def save_reminder_days(request):
    settings_obj = models.PlanSettings.load()
    try:
        days = int(request.POST.get('reminder_days', ''))
    except ValueError:
        days = None
    if days not in REMINDER_DAY_CHOICES:
        messages.error(request, 'Choose 7, 14, 30 or 60 days.')
        return _back(request)
    models.SettingsChangeLog.record(request.user, 'reminder_days', settings_obj.reminder_days, days)
    settings_obj.reminder_days = days
    settings_obj.save()
    messages.success(request, f'Reminders now start {days} days before each order-by date.')
    return _back(request)


@require_POST
@viewer_required
def toggle_digest(request):
    sub = models.WeeklyDigestSubscription.objects.filter(user=request.user)
    if sub.exists():
        sub.delete()
        messages.success(request, 'You will no longer get the Monday email.')
    elif not request.user.email:
        messages.error(request, 'Your account has no email address. Ask an administrator to add one.')
    else:
        models.WeeklyDigestSubscription.objects.create(user=request.user)
        messages.success(request, f'You will get this list every Monday at {request.user.email}.')
    return _back(request)


@require_POST
@planner_required
def refresh(request):
    if not sync.acumatica_configured():
        messages.error(request, 'Acumatica is not connected yet. The planner is showing the sample or imported data.')
        return _back(request)
    try:
        run = sync.run_sync(user=request.user)
    except sync.SyncInProgress as exc:
        messages.warning(request, str(exc))
    except (AcumaticaError, loaders.DataLoadError) as exc:
        messages.error(request, f'Refresh failed; the previous data is still shown. {exc}')
    else:
        messages.success(request, f'Refreshed from Acumatica ({sum(run.counts.values()):,} rows).')
    return _back(request)


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
