import os
from decimal import ROUND_HALF_UP, Decimal

from django import template

from planning.models import FLAG_LABELS

register = template.Library()


@register.simple_tag
def static_v(path):
    """A static file's URL with its last-changed time on the end, so browsers and caches fetch the new
    copy as soon as the file changes instead of keeping an old one."""
    from django.contrib.staticfiles import finders
    from django.templatetags.static import static
    url = static(path)
    found = finders.find(path)
    if found:
        try:
            return f'{url}?v={int(os.path.getmtime(found))}'
        except OSError:
            pass
    return url


def _q(value, places):
    return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


@register.filter
def plain_qty(value):
    """A quantity for an input box: no thousands separators, no trailing zeros."""
    if value is None:
        return ''
    d = _q(value, 2)
    return str(d.to_integral_value()) if d == d.to_integral_value() else format(d.normalize(), 'f')


@register.filter
def dom_id(value):
    """A safe element id fragment for a SKU (SKUs can hold characters that are not valid in ids)."""
    return str(value).encode('utf-8').hex()


@register.filter
def plain_num(value):
    """A number for a data attribute: full precision for the browser to calculate with."""
    return '' if value is None else format(Decimal(value).quantize(Decimal('0.000001')), 'f')


@register.filter
def qty(value, places=0):
    """Quantity with thousands separators; shows decimals only when needed."""
    if value is None:
        return '—'
    d = _q(value, max(2, int(places)))
    if d == d.to_integral_value():
        return f'{int(d):,}'
    return f'{d:,.{max(int(places), 1)}f}'.rstrip('0').rstrip('.')


@register.filter
def months(value):
    """Months of supply to one decimal; None means no usage (infinite)."""
    if value is None:
        return '∞'
    return f'{_q(value, 1):,}'


@register.filter
def lead_time(days):
    if days is None:
        return 'Unknown'
    days = int(days)
    if days >= 30 and days % 30 == 0:
        n = days // 30
        return f'{n} month' + ('s' if n != 1 else '')
    if days % 7 == 0 and days < 60:
        n = days // 7
        return f'{n} week' + ('s' if n != 1 else '')
    if days >= 60:
        return f'{_q(Decimal(days) / 30, 1)} months'
    return f'{days} days'


@register.filter
def get_item(mapping, key):
    return mapping.get(key)


@register.filter
def words(value):
    """snake_case identifier to plain words, e.g. trailing_invoices -> trailing shipments."""
    return str(value).replace('_', ' ')


@register.filter
def flag_label(flag):
    return FLAG_LABELS.get(flag, str(flag))


@register.filter
def status_class(status):
    """CSS modifier for a planning or reminder status."""
    value = getattr(status, 'value', status)
    return {
        'Stockout risk': 'danger', 'Order now': 'danger',
        'Order soon': 'warning', 'Reminder active': 'warning',
        'Covered': 'ok', 'On track': 'ok',
    }.get(value, 'neutral')


@register.simple_tag
def supply_bar(plan):
    """Bar fill plus two line positions (percent): the lead time (red) and the reorder point,
    lead time + cover months. The scale is reorder point x 1.6 so that line always sits at 62.5%."""
    scale = plan.target_months * Decimal('1.6')
    if scale <= 0:
        scale = Decimal(1)
    if plan.months_of_supply is None:
        fill = Decimal(100)
    else:
        fill = min(plan.months_of_supply / scale * 100, Decimal(100))
    return {
        'fill': f'{_q(fill, 1)}',
        'lead': f'{_q(plan.lead_time_months / scale * 100, 1)}',
        'reorder': f'{_q(plan.target_months / scale * 100, 1)}',
    }
