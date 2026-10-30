from decimal import ROUND_HALF_UP, Decimal

from django import template

from planning.models import FLAG_LABELS

register = template.Library()


def _q(value, places):
    return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


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
def money(value):
    if value is None:
        return '—'
    return f'${_q(value, 0):,.0f}'


@register.filter
def unit_cost(value):
    if value is None:
        return '—'
    return f'${_q(value, 2):,.2f}'


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
    """snake_case identifier to plain words, e.g. trailing_shipments -> trailing shipments."""
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
