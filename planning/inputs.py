"""Assemble planning inputs from plain row dicts.

The row shapes match the JSON import format (see sample_data/sample_data.json),
so both the Django app (from database rows) and the tests (from the sample
file) build inputs the same way.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from .models import BomLine, ComponentInput, FinishedGood
from .reminders import VendorInfo

Row = Mapping[str, Any]

COMPONENT = 'component'
FINISHED_GOOD = 'finished_good'


@dataclass
class PlanInputs:
    components: list[ComponentInput]
    bom_lines: list[BomLine]
    finished_goods: list[FinishedGood]
    vendors: list[VendorInfo]
    lead_time_sources: dict[str, str]    # sku -> 'override' | 'item_vendor' | 'vendor' | 'missing'


def to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == '':
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def to_date(value: Any) -> Optional[dt.date]:
    if value is None or value == '':
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def resolve_lead_time(
    override_days: Optional[int],
    item_vendor_days: Optional[int],
    vendor_days: Optional[int],
) -> tuple[Optional[int], str]:
    """Item override wins, then the item-vendor lead time, then the vendor default."""
    for days, source in ((override_days, 'override'), (item_vendor_days, 'item_vendor'), (vendor_days, 'vendor')):
        if days is not None:
            return int(days), source
    return None, 'missing'


def build_plan_inputs(
    *,
    stock_items: Iterable[Row],
    on_hand: Iterable[Row],
    open_po_lines: Iterable[Row],
    bom: Iterable[Row],
    vendors: Iterable[Row],
    item_vendors: Iterable[Row],
    po_history: Iterable[Row],
    fg_demand: Mapping[str, Decimal],
    lead_time_overrides: Optional[Mapping[str, int]] = None,
) -> PlanInputs:
    overrides = lead_time_overrides or {}
    vendor_rows = {v['vendor_id']: v for v in vendors}

    qty_on_hand: dict[str, Decimal] = defaultdict(Decimal)
    for row in on_hand:
        qty_on_hand[row['sku']] += to_decimal(row['qty']) or Decimal(0)

    qty_on_order: dict[str, Decimal] = defaultdict(Decimal)
    for row in open_po_lines:
        qty_on_order[row['sku']] += to_decimal(row['qty_open']) or Decimal(0)

    # Default vendor per item; if none is flagged default, use the first listed.
    item_vendor_for: dict[str, Row] = {}
    for row in item_vendors:
        current = item_vendor_for.get(row['sku'])
        if current is None or (row.get('is_default') and not current.get('is_default')):
            item_vendor_for[row['sku']] = row

    # Latest PO per (sku, vendor) and per sku
    last_po: dict[tuple[str, Optional[str]], Row] = {}
    for row in po_history:
        for key in ((row['sku'], row.get('vendor_id')), (row['sku'], None)):
            prev = last_po.get(key)
            if prev is None or to_date(row['order_date']) > to_date(prev['order_date']):
                last_po[key] = row

    items = list(stock_items)
    finished_goods = [
        FinishedGood(
            sku=item['sku'],
            description=item.get('description', ''),
            on_hand=qty_on_hand.get(item['sku'], Decimal(0)),
            demand_per_month=fg_demand.get(item['sku']),
        )
        for item in items if item['kind'] == FINISHED_GOOD
    ]

    components = []
    lead_time_sources = {}
    for item in items:
        if item['kind'] != COMPONENT:
            continue
        sku = item['sku']
        iv = item_vendor_for.get(sku)
        vendor_id = iv['vendor_id'] if iv else None
        vendor = vendor_rows.get(vendor_id) if vendor_id else None
        days, source = resolve_lead_time(
            overrides.get(sku),
            iv.get('lead_time_days') if iv else None,
            vendor.get('lead_time_days') if vendor else None,
        )
        lead_time_sources[sku] = source
        po = last_po.get((sku, vendor_id)) or last_po.get((sku, None))
        lot = to_decimal(iv.get('lot_size')) if iv else None
        components.append(ComponentInput(
            sku=sku,
            description=item.get('description', ''),
            vendor_id=vendor_id,
            vendor_name=vendor['name'] if vendor else vendor_id,
            on_hand=qty_on_hand.get(sku, Decimal(0)),
            on_order=qty_on_order.get(sku, Decimal(0)),
            lead_time_days=days,
            lot_size=lot or to_decimal(item.get('lot_size')),
            unit_cost=to_decimal(iv.get('last_cost')) if iv else None,
            last_po_qty=to_decimal(po['qty']) if po else None,
            last_po_date=to_date(po['order_date']) if po else None,
        ))

    bom_lines = [
        BomLine(fg_sku=row['fg_sku'], component_sku=row['component_sku'], qty_per=to_decimal(row['qty_per']))
        for row in bom
    ]
    vendor_infos = [
        VendorInfo(vendor_id=v['vendor_id'], name=v['name'], lead_time_days=v.get('lead_time_days'))
        for v in vendor_rows.values()
    ]
    vendor_infos.sort(key=lambda v: v.name)
    return PlanInputs(components, bom_lines, finished_goods, vendor_infos, lead_time_sources)
