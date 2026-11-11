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

from .models import BASIS_SIX, BomLine, ComponentInput, FinishedGood
from .reminders import VendorInfo

Row = Mapping[str, Any]

COMPONENT = 'component'
FINISHED_GOOD = 'finished_good'
PURCHASE = 'Purchase'      # StockItem.replenishment_source of items that are bought

# Lubricants / ingredients ("compounding") that are purchased outside this planner
EXCLUDED_SKU_PREFIXES = ('XLDXRX',)


def is_excluded(sku) -> bool:
    return bool(sku) and str(sku).strip().upper().startswith(EXCLUDED_SKU_PREFIXES)


@dataclass
class PlanInputs:
    components: list[ComponentInput]
    bom_lines: list[BomLine]
    finished_goods: list[FinishedGood]
    vendors: list[VendorInfo]
    uses_available: bool = False          # quantities are Acumatica "available", not on hand


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
    item_vendor_days: Optional[int],
    vendor_days: Optional[int],
) -> Optional[int]:
    """The lead time as Acumatica has it: the item-vendor lead time, else the vendor default."""
    for days in (item_vendor_days, vendor_days):
        if days is not None:
            return int(days)
    return None


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
    issue_demand: Optional[Mapping[str, Decimal]] = None,
    unproduced_orders: Optional[Mapping[str, Decimal]] = None,
    last_production: Optional[Mapping[str, tuple[Decimal, dt.date]]] = None,
    issue_history: Optional[Mapping[str, tuple[Decimal, int]]] = None,
    fg_history: Optional[Mapping[str, tuple[Decimal, int]]] = None,
    usage_choices: Optional[Mapping[str, tuple[str, Optional[Decimal]]]] = None,
    warning_acks: Optional[Mapping[str, frozenset]] = None,
) -> PlanInputs:
    stock_items = [r for r in stock_items if not is_excluded(r['sku'])]
    on_hand = [r for r in on_hand if not is_excluded(r['sku'])]
    open_po_lines = [r for r in open_po_lines if not is_excluded(r['sku'])]
    bom = [r for r in bom if not is_excluded(r['fg_sku']) and not is_excluded(r['component_sku'])]
    item_vendors = [r for r in item_vendors if not is_excluded(r['sku'])]
    po_history = [r for r in po_history if not is_excluded(r['sku'])]
    vendor_rows = {v['vendor_id']: v for v in vendors}

    qty_on_hand: dict[str, Decimal] = defaultdict(Decimal)
    for row in on_hand:
        qty_on_hand[row['sku']] += to_decimal(row['qty']) or Decimal(0)

    items = list(stock_items)
    # Prefer Acumatica's available quantity when it was synced; otherwise use on hand
    uses_available = any(item.get('qty_available') is not None for item in items)
    if uses_available:
        qty_on_hand = defaultdict(Decimal, {item['sku']: to_decimal(item.get('qty_available')) or Decimal(0)
                                            for item in items})

    qty_on_order: dict[str, Decimal] = defaultdict(Decimal)
    on_order_since: dict[str, dt.date] = {}
    earliest_promised: dict[str, dt.date] = {}
    for row in open_po_lines:
        promised = to_date(row.get('promised_date'))
        if promised and (row['sku'] not in earliest_promised or promised < earliest_promised[row['sku']]):
            earliest_promised[row['sku']] = promised
        qty_on_order[row['sku']] += to_decimal(row['qty_open']) or Decimal(0)
        ordered = to_date(row.get('order_date'))
        if ordered and (row['sku'] not in on_order_since or ordered < on_order_since[row['sku']]):
            on_order_since[row['sku']] = ordered

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

    finished_goods = [
        FinishedGood(
            sku=item['sku'],
            description=item.get('description', ''),
            on_hand=qty_on_hand.get(item['sku'], Decimal(0)),
            demand_per_month=fg_demand.get(item['sku']),
            last_run_qty=(last_production or {}).get(item['sku'], (None, None))[0],
            last_run_date=(last_production or {}).get(item['sku'], (None, None))[1],
            unproduced_orders=(unproduced_orders or {}).get(item['sku'], Decimal(0)),
        )
        for item in items if item['kind'] == FINISHED_GOOD
    ]

    components = []
    for item in items:
        sku = item['sku']
        iv = item_vendor_for.get(sku)
        # A finished good with Source = Purchase and a vendor is bought, not built: it is planned like a
        # component, with its invoices + kit breakdowns as usage
        purchased_fg = (item['kind'] == FINISHED_GOOD and iv
                        and item.get('replenishment_source') in (PURCHASE, ''))
        if item['kind'] != COMPONENT and not purchased_fg:
            continue
        vendor_id = iv['vendor_id'] if iv else None
        vendor = vendor_rows.get(vendor_id) if vendor_id else None
        days = resolve_lead_time(
            iv.get('lead_time_days') if iv else None,
            vendor.get('lead_time_days') if vendor else None,
        )
        long_term, active = ((fg_history if purchased_fg else issue_history) or {}).get(sku, (None, 0))
        if purchased_fg:
            # invoices + kit breakdowns (fg_demand); inventory issues are not counted for finished goods
            usage = fg_demand.get(sku) or Decimal(0)
        else:
            usage = (issue_demand or {}).get(sku)
        po = last_po.get((sku, vendor_id)) or last_po.get((sku, None))
        # Vendor check: is there a default, and did the latest PO go to it?
        rows_for_sku = [row for row in item_vendors if row['sku'] == sku]
        default_row = next((row for row in rows_for_sku if row.get('is_default')), None)
        default_id = default_row['vendor_id'] if default_row else None
        latest = last_po.get((sku, None))
        latest_vendor = latest.get('vendor_id') if latest else None
        lot = to_decimal(iv.get('lot_size')) if iv else None
        components.append(ComponentInput(
            sku=sku,
            description=item.get('description', ''),
            vendor_id=vendor_id,
            vendor_name=vendor['name'] if vendor else vendor_id,
            on_hand=qty_on_hand.get(sku, Decimal(0)),
            on_order=qty_on_order.get(sku, Decimal(0)),
            in_receiving=max(to_decimal(item.get('qty_receiving')) or Decimal(0), Decimal(0)),
            lead_time_days=days,
            lot_size=lot or to_decimal(item.get('lot_size')),
            last_po_qty=to_decimal(po['qty']) if po else None,
            last_po_date=to_date(po['order_date']) if po else None,
            on_order_since=on_order_since.get(sku),
            earliest_promised=earliest_promised.get(sku),
            issue_demand_per_month=usage,
            is_finished_good=bool(purchased_fg),
            long_term_usage=long_term,
            default_vendor_id=default_id,
            default_vendor_name=(vendor_rows.get(default_id) or {}).get('name') if default_id else None,
            last_po_vendor_id=latest_vendor,
            last_po_vendor_name=(vendor_rows.get(latest_vendor) or {}).get('name') if latest_vendor else None,
            has_vendor_rows=bool(rows_for_sku),
            acked_warnings=frozenset((warning_acks or {}).get(sku, ())),
            usage_basis=(usage_choices or {}).get(sku, (BASIS_SIX, None))[0],
            custom_usage=(usage_choices or {}).get(sku, (BASIS_SIX, None))[1],
            active_months=active,
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
    return PlanInputs(components, bom_lines, finished_goods, vendor_infos, uses_available)
