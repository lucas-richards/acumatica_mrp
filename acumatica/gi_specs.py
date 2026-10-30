"""Field lists for each Generic Inquiry and mapping of GI rows to import-format dicts.

The GI column captions must match `gi_column` exactly (OData property names are
the column captions without spaces). docs/acumatica_generic_inquiries.md is the
human-readable version of this file for the Acumatica admin.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

from .odata import AcumaticaError


class GIFieldError(AcumaticaError):
    pass


def _str(v: Any) -> str:
    return '' if v is None else str(v).strip()


def _decimal(v: Any) -> Decimal | None:
    if v is None or v == '':
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        raise ValueError(f'not a number: {v!r}') from None


def _int(v: Any) -> int | None:
    d = _decimal(v)
    return None if d is None else int(d)


def _date(v: Any) -> dt.date | None:
    if v is None or v == '':
        return None
    s = str(v)
    if s.startswith('/Date('):   # OData v2/v3 verbose: /Date(1730160000000)/
        millis = int(s[6:].split(')')[0].split('+')[0].split('-')[0])
        return dt.datetime.fromtimestamp(millis / 1000, tz=dt.timezone.utc).date()
    return dt.date.fromisoformat(s[:10])


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return _str(v).lower() in ('true', '1', 'yes', 'y')


@dataclass(frozen=True)
class Field:
    local: str
    gi_column: str
    convert: Callable[[Any], Any]
    required: bool = True
    note: str = ''


@dataclass(frozen=True)
class GISpec:
    key: str                 # dataset key; GI name comes from settings ACUMATICA['GI_NAMES'][key]
    default_gi_name: str
    purpose: str
    fields: tuple[Field, ...]

    @property
    def select(self) -> list[str]:
        return [f.gi_column for f in self.fields]


SPECS: dict[str, GISpec] = {s.key: s for s in [
    GISpec('vendors', 'MRP-Vendors', 'Active vendors with their default purchasing lead time.', (
        Field('vendor_id', 'VendorID', _str),
        Field('name', 'VendorName', _str),
        Field('lead_time_days', 'LeadTimeDays', _int, required=False,
              note='Vendor default lead time in days (Vendors > Purchase settings > Lead Time)'),
        Field('email', 'Email', _str, required=False),
    )),
    GISpec('stock_items', 'MRP-StockItems', 'Active stock items: purchased components and finished goods.', (
        Field('sku', 'InventoryID', _str),
        Field('description', 'Description', _str, required=False),
        Field('item_class', 'ItemClass', _str, required=False),
        Field('is_finished_good', 'IsFinishedGood', _bool,
              note="Calculated column, e.g. =IIf([InventoryItem.ItemClassID]='FINISHED', 1, 0), or Replenishment Source = Manufacturing"),
        Field('uom', 'BaseUnit', _str, required=False),
        Field('lot_size', 'LotSize', _decimal, required=False,
              note='Order multiple / lot size from Item Warehouse Details or Replenishment settings'),
    )),
    GISpec('on_hand', 'MRP-OnHand', 'Quantity on hand by item and warehouse.', (
        Field('sku', 'InventoryID', _str),
        Field('warehouse', 'Warehouse', _str),
        Field('qty', 'QtyOnHand', _decimal),
    )),
    GISpec('open_po_lines', 'MRP-OpenPOLines', 'Open purchase order lines (status Open, not fully received).', (
        Field('order_nbr', 'OrderNbr', _str),
        Field('line_nbr', 'LineNbr', _int),
        Field('sku', 'InventoryID', _str),
        Field('vendor_id', 'VendorID', _str),
        Field('qty_open', 'OpenQty', _decimal, note='Base-unit open quantity (Order Qty - Received Qty)'),
        Field('promised_date', 'PromisedDate', _date, required=False),
    )),
    GISpec('bom', 'MRP-BOM', 'Material lines of the active BOM revision for each finished good.', (
        Field('fg_sku', 'FinishedGood', _str),
        Field('component_sku', 'Component', _str),
        Field('qty_per', 'QtyPerUnit', _decimal, note='QtyReq / BOM batch size, in base units'),
    )),
    GISpec('item_vendors', 'MRP-ItemVendors', 'Item-vendor settings (Stock Items > Vendors tab).', (
        Field('sku', 'InventoryID', _str),
        Field('vendor_id', 'VendorID', _str),
        Field('is_default', 'IsDefault', _bool),
        Field('lead_time_days', 'LeadTimeDays', _int, required=False,
              note='Item-specific lead time; blank = use the vendor default'),
        Field('last_cost', 'LastCost', _decimal, required=False, note='Last purchase price in base units'),
        Field('lot_size', 'LotSize', _decimal, required=False, note='Purchase lot size / order multiple'),
    )),
    GISpec('po_history', 'MRP-POHistory', 'Purchase order lines from the last 24 months (any status except cancelled).', (
        Field('sku', 'InventoryID', _str),
        Field('vendor_id', 'VendorID', _str),
        Field('order_nbr', 'OrderNbr', _str),
        Field('order_date', 'OrderDate', _date),
        Field('qty', 'OrderQty', _decimal),
        Field('unit_cost', 'UnitCost', _decimal, required=False),
    )),
    GISpec('demand_shipments', 'MRP-DemandShipments', 'Finished-good shipment lines (confirmed), last 12 months.', (
        Field('sku', 'InventoryID', _str),
        Field('date', 'ShipmentDate', _date),
        Field('qty', 'ShippedQty', _decimal),
    )),
    GISpec('demand_sales_orders', 'MRP-DemandSalesOrders', 'Open sales order lines for finished goods.', (
        Field('sku', 'InventoryID', _str),
        Field('date', 'RequestDate', _date),
        Field('qty', 'OpenQty', _decimal),
    )),
    GISpec('demand_forecast', 'MRP-DemandForecast', 'Finished-good demand forecast by period.', (
        Field('sku', 'InventoryID', _str),
        Field('date', 'PeriodStart', _date),
        Field('qty', 'ForecastQty', _decimal),
    )),
]}

# Demand dataset key -> planning demand source name
DEMAND_DATASETS = {
    'trailing_shipments': 'demand_shipments',
    'open_sales_orders': 'demand_sales_orders',
    'forecast': 'demand_forecast',
}


def map_rows(spec: GISpec, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert raw OData rows into import-format dicts, validating columns and types."""
    rows = list(rows)
    if rows:
        missing = [f.gi_column for f in spec.fields if f.required and f.gi_column not in rows[0]]
        if missing:
            raise GIFieldError(f'GI {spec.default_gi_name!r} is missing columns: {", ".join(missing)}')
    out = []
    for index, row in enumerate(rows, start=1):
        item = {}
        for f in spec.fields:
            try:
                value = f.convert(row.get(f.gi_column))
            except ValueError as exc:
                raise GIFieldError(f'GI {spec.default_gi_name!r} row {index} column {f.gi_column}: {exc}') from None
            if f.required and value in (None, ''):
                raise GIFieldError(f'GI {spec.default_gi_name!r} row {index}: {f.gi_column} is empty')
            item[f.local] = value
        out.append(item)
    if spec.key == 'stock_items':
        for item in out:
            item['kind'] = 'finished_good' if item.pop('is_finished_good') else 'component'
    return out
