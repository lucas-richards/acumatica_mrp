"""Read Acumatica entities over REST and map them to the planner's import format.

Datasets produced (same shapes as sample_data/sample_data.json):
vendors, stock_items, on_hand, open_po_lines, bom, item_vendors, po_history,
plus dated demand lines per demand source.

Rules:
- Only items in an active BOM tree are kept. Items with a BOM (finished goods
  and subassemblies) are "finished_good"; leaf materials are "component".
- Multi-level BOMs are exploded to purchased leaf components, so a finished
  good's qty-per includes components inside its subassemblies. Each subassembly
  also keeps its own lines, so subassembly stock counts as embedded stock.
- All quantities and costs are converted to the item's base unit using the
  item's own unit conversions; lines whose unit can't be converted are skipped
  and reported as warnings.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional, Protocol

from .rest import AcumaticaError, AcumaticaPermissionError

log = logging.getLogger('acumatica')

OPEN_PO_STATUSES = ('Open', 'Pending Printing', 'Pending Email')
STOCK_PO_LINE_TYPES = ('Goods for IN', 'Goods for Inventory', 'Goods for Manufacturing', 'Goods for SO')
SHIPPED_STATUSES = ('Confirmed', 'Completed')
OPEN_SO_STATUSES = ('Open', 'Back Order')
MAX_BOM_DEPTH = 12

DEMAND_SOURCE_ENTITIES = {
    'trailing_shipments': 'Shipment',
    'open_sales_orders': 'SalesOrder',
    'forecast': 'Forecast',
}


class Client(Protocol):
    def fetch(self, endpoint: str, entity: str, *, select=None, expand=None, filter: str = '') -> list[dict]: ...


@dataclass
class FetchOptions:
    today: dt.date
    demand_sources: list[str]
    po_history_months: int = 24
    shipment_months: int = 13
    sales_order_types: tuple[str, ...] = ('SO',)


@dataclass
class FetchResult:
    data: dict[str, list[dict]]
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def dec(value: Any) -> Optional[Decimal]:
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def to_date(value: Any) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def odata_date(day: dt.date) -> str:
    return f"datetimeoffset'{day.isoformat()}T00:00:00Z'"


def months_ago(day: dt.date, months: int) -> dt.date:
    index = day.year * 12 + (day.month - 1) - months
    return dt.date(index // 12, index % 12 + 1, 1)


def any_of(field_name: str, values: Iterable[str]) -> str:
    return '(' + ' or '.join(f"{field_name} eq '{v}'" for v in values) + ')'


class UnitIssues:
    """Collects lines skipped for an unknown unit, reported once per (document type, item, unit)."""

    def __init__(self):
        self.seen: dict[tuple[str, str, str], list] = {}

    def add(self, kind: str, sku: str, uom: Optional[str], ref: Any) -> None:
        entry = self.seen.setdefault((kind, sku, uom or ''), [0, ref])
        entry[0] += 1

    def messages(self) -> list[str]:
        return [f'{kind}: {count} line(s) for {sku} in unit {uom} skipped — no conversion to the base unit '
                f'(e.g. {ref}). Add the conversion on the stock item in Acumatica.'
                for (kind, sku, uom), (count, ref) in sorted(self.seen.items())]


class Units:
    """Per-item unit conversions to the base unit."""

    def __init__(self):
        self.base: dict[str, str] = {}
        self.factors: dict[str, dict[str, Decimal]] = {}

    def add_item(self, sku: str, base_uom: str, conversions: Iterable[dict]) -> None:
        self.base[sku] = base_uom or ''
        factors = {(base_uom or '').upper(): Decimal(1)}
        for c in conversions or []:
            to_uom = (c.get('ToUOM') or '').upper()
            from_uom = (c.get('FromUOM') or '').upper()
            factor = dec(c.get('ConversionFactor'))
            if to_uom != (base_uom or '').upper() or not from_uom or not factor:
                continue
            factors[from_uom] = factor if (c.get('MultiplyOrDivide') or 'Multiply') == 'Multiply' else Decimal(1) / factor
        self.factors[sku] = factors

    def factor(self, sku: str, uom: Optional[str]) -> Optional[Decimal]:
        """Base units per one `uom`; None if unknown. A blank unit means the base unit."""
        if sku not in self.factors:
            return None
        if not uom:
            return Decimal(1)
        return self.factors[sku].get(uom.upper())

    def qty(self, sku: str, qty: Any, uom: Optional[str]) -> Optional[Decimal]:
        q, f = dec(qty), self.factor(sku, uom)
        return None if q is None or f is None else q * f

    def unit_cost(self, sku: str, cost: Any, uom: Optional[str]) -> Optional[Decimal]:
        c, f = dec(cost), self.factor(sku, uom)
        return None if c is None or not f else c / f


# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------

def fetch_boms(client: Client, today: dt.date) -> list[dict]:
    try:
        return client.fetch(
            'manufacturing', 'BillOfMaterial',
            filter="Status eq 'Active'",
            expand=['Operations/Material'],
            select=['BOMID', 'Revision', 'InventoryID', 'Status', 'EffStartDate', 'EffEndDate',
                    'Operations/OperationNbr', 'Operations/Material/InventoryID', 'Operations/Material/QtyRequired',
                    'Operations/Material/BatchSize', 'Operations/Material/UOM',
                    'Operations/Material/EffectiveDate', 'Operations/Material/ExpirationDate'],
        )
    except AcumaticaPermissionError as exc:
        raise AcumaticaPermissionError(
            'Cannot read Bills of Material. Ask your Acumatica admin to give the planner user access to the '
            f'Bill of Material screen (AM208000). Details: {exc}', exc.status_code) from exc


def choose_boms(boms: list[dict], today: dt.date, warnings: list[str]) -> dict[str, dict]:
    """One BOM per item: the active revision in effect today with the latest start date."""
    by_item: dict[str, list[dict]] = defaultdict(list)
    for bom in boms:
        start, end = to_date(bom.get('EffStartDate')), to_date(bom.get('EffEndDate'))
        if (start is None or start <= today) and (end is None or end >= today) and bom.get('InventoryID'):
            by_item[bom['InventoryID'].strip()].append(bom)
    chosen = {}
    for sku, candidates in by_item.items():
        candidates.sort(key=lambda b: (to_date(b.get('EffStartDate')) or dt.date.min, b.get('BOMID') or '',
                                       b.get('Revision') or ''), reverse=True)
        ids = {b.get('BOMID') for b in candidates}
        if len(ids) > 1:
            warnings.append(f'{sku} has {len(ids)} active BOMs ({", ".join(sorted(filter(None, ids)))}); '
                            f'using {candidates[0].get("BOMID")} rev {candidates[0].get("Revision")}.')
        chosen[sku] = candidates[0]
    return chosen


def direct_lines(bom: dict, today: dt.date) -> list[tuple[str, Decimal, Optional[str]]]:
    """(component, qty per one parent in material UOM, UOM) for a BOM, summed over operations."""
    totals: dict[tuple[str, Optional[str]], Decimal] = defaultdict(Decimal)
    for op in bom.get('Operations') or []:
        for m in op.get('Material') or []:
            sku = (m.get('InventoryID') or '').strip()
            qty = dec(m.get('QtyRequired'))
            if not sku or not qty:
                continue
            eff, exp = to_date(m.get('EffectiveDate')), to_date(m.get('ExpirationDate'))
            if (eff and eff > today) or (exp and exp < today):
                continue
            batch = dec(m.get('BatchSize')) or Decimal(1)
            totals[(sku, m.get('UOM'))] += qty / batch
    return [(sku, qty, uom) for (sku, uom), qty in totals.items()]


def explode(direct: dict[str, list[tuple[str, Decimal]]], warnings: list[str]) -> dict[str, dict[str, Decimal]]:
    """Leaf component quantities per one unit of every BOM parent (multi-level)."""
    memo: dict[str, dict[str, Decimal]] = {}

    def walk(item: str, path: tuple[str, ...]) -> dict[str, Decimal]:
        if item in memo:
            return memo[item]
        if item in path or len(path) > MAX_BOM_DEPTH:
            warnings.append(f'BOM loop or excessive depth at {" > ".join(path + (item,))}; branch skipped.')
            return {}
        leaves: dict[str, Decimal] = defaultdict(Decimal)
        for child, qty in direct.get(item, []):
            if child in direct:
                for leaf, leaf_qty in walk(child, path + (item,)).items():
                    leaves[leaf] += qty * leaf_qty
            else:
                leaves[child] += qty
        memo[item] = dict(leaves)
        return memo[item]

    return {parent: walk(parent, ()) for parent in direct}


# ---------------------------------------------------------------------------
# Main fetch
# ---------------------------------------------------------------------------

STOCK_ITEM_SELECT = [
    'InventoryID', 'Description', 'ItemClass', 'ItemType', 'ItemStatus', 'BaseUOM', 'LastCost',
    'UOMConversions/FromUOM', 'UOMConversions/ToUOM', 'UOMConversions/MultiplyOrDivide',
    'UOMConversions/ConversionFactor',
    'WarehouseDetails/WarehouseID', 'WarehouseDetails/QtyOnHand',
    'VendorDetails/VendorID', 'VendorDetails/Default', 'VendorDetails/Active', 'VendorDetails/Warehouse',
    'VendorDetails/LeadTimeDays', 'VendorDetails/AddLeadTimeDays', 'VendorDetails/LastVendorPrice',
    'VendorDetails/LotSize', 'VendorDetails/PurchaseUnit',
]


def fetch_all(client: Client, options: FetchOptions) -> FetchResult:
    today = options.today
    warnings: list[str] = []
    data: dict[str, list[dict]] = {}

    # 1. BOMs (fail first and fast if access is missing)
    boms = choose_boms(fetch_boms(client, today), today, warnings)

    # 2. Stock items with units, on hand and vendor settings
    items = {(r.get('InventoryID') or '').strip(): r for r in client.fetch(
        'default', 'StockItem', filter="ItemStatus ne 'Inactive'",
        expand=['UOMConversions', 'WarehouseDetails', 'VendorDetails'], select=STOCK_ITEM_SELECT)}
    units = Units()
    for sku, item in items.items():
        units.add_item(sku, item.get('BaseUOM'), item.get('UOMConversions'))

    unit_issues = UnitIssues()

    # BOM lines in base units, then multi-level explosion. Materials that are not
    # active stock items (non-stock packaging, inactive parts) have no stock to plan.
    not_stock: set[str] = set()
    direct: dict[str, list[tuple[str, Decimal]]] = {}
    for parent, bom in boms.items():
        lines = []
        for child, qty, uom in direct_lines(bom, today):
            if child not in items:
                not_stock.add(child)
                continue
            base_qty = units.qty(child, qty, uom)
            if base_qty is None:
                unit_issues.add('BOM', child, uom, parent)
                continue
            lines.append((child, base_qty))
        direct[parent] = lines
    exploded = explode(direct, warnings)
    parents = set(exploded)
    components = {c for leaves in exploded.values() for c in leaves}
    relevant = parents | components

    missing = relevant - set(items)
    not_stock |= missing
    relevant -= missing
    parents &= relevant
    if not_stock:
        names = sorted(not_stock)
        warnings.append(f'{len(names)} BOM item(s) are non-stock or inactive and are not planned: '
                        + ', '.join(names[:30]) + (' …' if len(names) > 30 else ''))
    data['bom'] = [{'fg_sku': p, 'component_sku': c, 'qty_per': q}
                   for p, leaves in exploded.items() if p in relevant
                   for c, q in leaves.items() if q > 0 and c in relevant]

    data['stock_items'] = [{
        'sku': sku,
        'description': items[sku].get('Description') or '',
        'item_class': items[sku].get('ItemClass') or '',
        'kind': 'finished_good' if sku in parents else 'component',
        'uom': items[sku].get('BaseUOM') or '',
        'lot_size': None,
    } for sku in sorted(relevant)]

    data['on_hand'] = [
        {'sku': sku, 'warehouse': w.get('WarehouseID') or '', 'qty': dec(w.get('QtyOnHand')) or Decimal(0)}
        for sku in sorted(relevant) for w in items[sku].get('WarehouseDetails') or []
        if dec(w.get('QtyOnHand'))
    ]

    data['item_vendors'] = []
    for sku in sorted(relevant & components):
        item = items[sku]
        per_vendor: dict[str, dict] = {}
        for vd in item.get('VendorDetails') or []:
            vendor = (vd.get('VendorID') or '').strip()
            if not vendor or vd.get('Active') is False:
                continue
            # Prefer the all-warehouses line over warehouse-specific ones
            if vendor in per_vendor and not per_vendor[vendor].get('Warehouse'):
                continue
            per_vendor[vendor] = vd
        for vendor, vd in per_vendor.items():
            purchase_unit = vd.get('PurchaseUnit')
            lead = vd.get('LeadTimeDays')
            cost = units.unit_cost(sku, vd.get('LastVendorPrice'), purchase_unit) if dec(vd.get('LastVendorPrice')) else None
            if cost is None:
                cost = dec(item.get('LastCost')) or None
            lot = units.qty(sku, vd.get('LotSize'), purchase_unit) if dec(vd.get('LotSize')) else None
            data['item_vendors'].append({
                'sku': sku, 'vendor_id': vendor, 'is_default': bool(vd.get('Default')),
                'lead_time_days': None if lead is None else int(lead) + int(vd.get('AddLeadTimeDays') or 0),
                'last_cost': cost, 'lot_size': lot or None,
            })

    # 3. Vendors
    data['vendors'] = [{
        'vendor_id': (v.get('VendorID') or '').strip(), 'name': v.get('VendorName') or v.get('VendorID'),
        'lead_time_days': v.get('LeadTimedays') or None, 'email': '',
    } for v in client.fetch('default', 'Vendor', filter="Status eq 'Active'",
                            select=['VendorID', 'VendorName', 'Status', 'LeadTimedays'])]

    # 4. Purchase orders: open lines and history
    since = months_ago(today, options.po_history_months)
    pos = client.fetch(
        'default', 'PurchaseOrder',
        filter=f"Type eq 'Normal' and ({any_of('Status', OPEN_PO_STATUSES)[1:-1]} or Date ge {odata_date(since)})",
        expand=['Details'],
        select=['OrderNbr', 'Type', 'Status', 'Date', 'VendorID', 'Details/LineNbr', 'Details/LineType',
                'Details/InventoryID', 'Details/UOM', 'Details/OrderQty', 'Details/QtyOnReceipts',
                'Details/UnitCost', 'Details/Promised', 'Details/Completed', 'Details/Cancelled'])
    data['open_po_lines'], data['po_history'] = [], []
    for po in pos:
        status, order_date = po.get('Status'), to_date(po.get('Date'))
        for line in po.get('Details') or []:
            sku = (line.get('InventoryID') or '').strip()
            if sku not in relevant or line.get('LineType') not in STOCK_PO_LINE_TYPES:
                continue
            qty = units.qty(sku, line.get('OrderQty'), line.get('UOM'))
            if qty is None:
                unit_issues.add('Purchase order', sku, line.get('UOM'), po.get('OrderNbr'))
                continue
            if status in OPEN_PO_STATUSES and not line.get('Completed') and not line.get('Cancelled'):
                received = units.qty(sku, line.get('QtyOnReceipts') or 0, line.get('UOM')) or Decimal(0)
                open_qty = qty - received
                if open_qty > 0:
                    data['open_po_lines'].append({
                        'order_nbr': po.get('OrderNbr'), 'line_nbr': line.get('LineNbr'), 'sku': sku,
                        'vendor_id': po.get('VendorID'), 'qty_open': open_qty,
                        'promised_date': to_date(line.get('Promised')),
                    })
            if status != 'Canceled' and not line.get('Cancelled') and order_date and order_date >= since:
                data['po_history'].append({
                    'sku': sku, 'vendor_id': po.get('VendorID'), 'order_nbr': po.get('OrderNbr'),
                    'order_date': order_date, 'qty': qty,
                    'unit_cost': units.unit_cost(sku, line.get('UnitCost'), line.get('UOM')),
                })

    # 5. Finished-good demand
    data['demand'] = []
    dated: dict[str, list[dict]] = {}
    for source in options.demand_sources:
        if source == 'trailing_shipments':
            dated[source] = _shipments(client, options, parents, units, unit_issues)
        elif source == 'open_sales_orders':
            dated[source] = _sales_orders(client, options, parents, units, unit_issues)
        elif source == 'forecast':
            raise AcumaticaError('The forecast demand source is not available from Acumatica yet '
                                 '(the planner user has no access to the Forecast screen AM202000).')
        else:
            raise AcumaticaError(f'Unknown demand source {source!r}')
    for source, rows in dated.items():
        data['demand'] += [{'sku': r['sku'], 'source': source, 'month': r['date'], 'qty': r['qty']} for r in rows]

    warnings += unit_issues.messages()
    for w in warnings:
        log.warning(w)
    return FetchResult(data=data, warnings=warnings)


def _shipments(client, options, parents, units, unit_issues) -> list[dict]:
    since = months_ago(options.today, options.shipment_months)
    rows = []
    for s in client.fetch(
            'default', 'Shipment',
            filter=(f"Type eq 'Shipment' and Operation eq 'Issue' and ShipmentDate ge {odata_date(since)} "
                    f"and {any_of('Status', SHIPPED_STATUSES)}"),
            expand=['Details'],
            select=['ShipmentNbr', 'ShipmentDate', 'Status', 'Details/InventoryID', 'Details/ShippedQty',
                    'Details/UOM']):
        day = to_date(s.get('ShipmentDate'))
        for line in s.get('Details') or []:
            sku = (line.get('InventoryID') or '').strip()
            if sku not in parents or not day:
                continue
            qty = units.qty(sku, line.get('ShippedQty'), line.get('UOM'))
            if qty is None:
                unit_issues.add('Shipment', sku, line.get('UOM'), s.get('ShipmentNbr'))
                continue
            rows.append({'sku': sku, 'date': day, 'qty': qty})
    return rows


def _sales_orders(client, options, parents, units, unit_issues) -> list[dict]:
    rows = []
    for so in client.fetch(
            'default', 'SalesOrder',
            filter=f"{any_of('OrderType', options.sales_order_types)} and {any_of('Status', OPEN_SO_STATUSES)}",
            expand=['Details'],
            select=['OrderType', 'OrderNbr', 'Status', 'RequestedOn', 'Details/InventoryID', 'Details/OpenQty',
                    'Details/UOM', 'Details/RequestedOn', 'Details/Operation', 'Details/Completed']):
        for line in so.get('Details') or []:
            sku = (line.get('InventoryID') or '').strip()
            if sku not in parents or line.get('Completed') or line.get('Operation') not in (None, 'Issue'):
                continue
            day = to_date(line.get('RequestedOn')) or to_date(so.get('RequestedOn'))
            qty = units.qty(sku, line.get('OpenQty'), line.get('UOM'))
            if qty is None:
                unit_issues.add('Sales order', sku, line.get('UOM'), so.get('OrderNbr'))
                continue
            if day and qty > 0:
                rows.append({'sku': sku, 'date': day, 'qty': qty})
    return rows
