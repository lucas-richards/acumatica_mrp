"""CSV export of the plan, grouped by vendor."""
from __future__ import annotations

import csv
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby

from planning.models import FLAG_LABELS

CSV_COLUMNS = [
    'Vendor', 'Vendor ID', 'Component', 'Description', 'Status', 'Monthly usage (2 year)', 'Months with use (of 24)', 'Monthly usage (6 months)', 'Monthly usage (custom)', 'Usage in use', 'Lead time (days)',
    'Available / on hand', 'On order', 'On order since', 'In receiving', 'Months of supply',
    'Reorder point (months)',
    'Order by', 'Order qty', 'Last order qty', 'Last order date', 'Months left when an order arrives', 'Months left with the suggested order', 'Impact (finished goods)', 'Flags',
]


def _safe(value):
    """Stop spreadsheet formula injection from ERP text fields."""
    text = '' if value is None else str(value)
    return "'" + text if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text


def _num(value, places=2):
    if value is None:
        return ''
    d = Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return format(d.normalize(), 'f')


def write_plan_csv(out, result) -> None:
    writer = csv.writer(out)
    writer.writerow([f'Purchase plan {result.today:%Y-%m-%d}',
                     'Order qty covers lead time + time to prepare',
                     f'Demand: {result.demand_source.replace("_", " ")}'])
    writer.writerow(CSV_COLUMNS)

    def vendor_key(p):
        return (p.component.vendor_name or 'No vendor', p.component.vendor_id or '')

    for (vendor_name, vendor_id), rows in groupby(sorted(result.plans, key=vendor_key), key=vendor_key):
        rows = list(rows)   # already in urgency order within each vendor (stable sort)
        for p in rows:
            c = p.component
            writer.writerow([
                _safe(vendor_name), _safe(vendor_id), _safe(c.sku), _safe(c.description), p.status.value,
                _num(c.long_term_usage) if c.long_term_usage is not None else '', c.active_months if c.long_term_usage is not None else '',
                _num(c.issue_demand_per_month or 0), _num(c.custom_usage) if c.custom_usage is not None else '',
                {'six': '6 months', 'two_year': '2 year', 'custom': 'custom'}[p.usage_basis],
                c.lead_time_days if c.lead_time_days is not None else '',
                _num(c.on_hand), _num(c.on_order),
                c.on_order_since.isoformat() if c.on_order_since else '', _num(c.in_receiving),
                'back ordered' if c.on_hand < 0 else (_num(p.months_of_supply, 1) if p.months_of_supply is not None else 'no usage'),
                _num(p.target_months, 1),
                ('Now' if p.order_now else p.order_by_date.isoformat()) if p.order_by_date else '',
                _num(p.suggested_qty),
                _num(c.last_po_qty), c.last_po_date.isoformat() if c.last_po_date else '',
                (_num(p.months_at_arrival, 1) if p.months_at_arrival is not None else f'need {p.missing_for_months_left}'),
                (_num(p.months_at_arrival_with(p.suggested_qty), 1) if p.months_at_arrival is not None
                 else f'need {p.missing_for_months_left}'),
                ', '.join(_safe(r.fg_sku) for r in p.breakdown),
                '; '.join(sorted(FLAG_LABELS[f] for f in p.flags)),
            ])
        to_order = [p for p in rows if p.needs_order]
        writer.writerow([_safe(vendor_name), _safe(vendor_id), 'SUBTOTAL', f'{len(to_order)} to order',
                         *[''] * 21])

