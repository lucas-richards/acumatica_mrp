"""CSV export of the plan (grouped by vendor) and an iCalendar file of vendor order-by dates."""
from __future__ import annotations

import csv
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby

from django.utils import timezone

from planning.models import FLAG_LABELS

CSV_COLUMNS = [
    'Vendor', 'Vendor ID', 'Component', 'Description', 'Status', 'Usage per month', 'Lead time (days)',
    'Demand in lead time', 'On hand', 'On order', 'In FG stock', 'Months of supply', 'Target months',
    'Order by', 'Last PO qty', 'Last PO date', 'Order qty', 'Unit cost', 'Estimated cost', 'Flags',
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
    writer.writerow([f'Purchase plan {result.today:%Y-%m-%d}', f'Cover +{result.options.cover_months} months',
                     'Qty mode: ' + ('Match last PO' if result.options.qty_mode.value == 'last_po' else 'To reach cover target'),
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
                _num(p.usage_per_month), c.lead_time_days if c.lead_time_days is not None else '',
                _num(p.demand_in_lead_time), _num(c.on_hand), _num(c.on_order), _num(p.embedded_stock),
                _num(p.months_of_supply, 1) if p.months_of_supply is not None else 'no usage',
                _num(p.target_months, 1),
                ('Now' if p.order_now else p.order_by_date.isoformat()) if p.order_by_date else '',
                _num(c.last_po_qty), c.last_po_date.isoformat() if c.last_po_date else '',
                _num(p.suggested_qty), _num(c.unit_cost, 4), _num(p.estimated_cost),
                '; '.join(sorted(FLAG_LABELS[f] for f in p.flags)),
            ])
        to_order = [p for p in rows if p.needs_order]
        total = sum((p.estimated_cost or Decimal(0) for p in to_order), Decimal(0))
        writer.writerow([_safe(vendor_name), _safe(vendor_id), 'SUBTOTAL', f'{len(to_order)} to order',
                         *[''] * 14, _num(total), ''])
        writer.writerow([])


def _ics_escape(text: str) -> str:
    return text.replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')


def order_by_calendar(result, url: str) -> str:
    """One all-day event per vendor with components due, with an alarm at the reminder window."""
    stamp = timezone.now().strftime('%Y%m%dT%H%M%SZ')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Westridge//MRP Planner//EN', 'CALSCALE:GREGORIAN',
             'X-WR-CALNAME:Purchasing order-by dates']
    for r in result.reminders:
        if not r.components_due or r.next_order_by is None:
            continue
        day = max(r.next_order_by, result.today)
        summary = f'Order from {r.vendor.name}' + (' (overdue)' if r.next_order_by < result.today else '')
        description = f'{r.components_due} component(s) due: {", ".join(r.due_skus)}\n{url}'
        lines += [
            'BEGIN:VEVENT',
            f'UID:mrp-{r.vendor.vendor_id}-{day:%Y%m%d}@westridge-mrp',
            f'DTSTAMP:{stamp}',
            f'DTSTART;VALUE=DATE:{day:%Y%m%d}',
            f'SUMMARY:{_ics_escape(summary)}',
            f'DESCRIPTION:{_ics_escape(description)}',
            'BEGIN:VALARM', 'ACTION:DISPLAY', f'DESCRIPTION:{_ics_escape(summary)}',
            f'TRIGGER:-P{result.reminder_days}D', 'END:VALARM',
            'END:VEVENT',
        ]
    lines.append('END:VCALENDAR')
    return '\r\n'.join(_fold(line) for line in lines) + '\r\n'


def _fold(line: str) -> str:
    """Fold lines longer than 75 octets as RFC 5545 requires."""
    out, current = [], ''
    for ch in line:
        if len((current + ch).encode()) > 75:
            out.append(current)
            current = ' ' + ch
        else:
            current += ch
    out.append(current)
    return '\r\n'.join(out)
